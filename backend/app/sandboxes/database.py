"""Database-engine protocol and the existing MySQL implementation.

This module is intentionally small.  Preview orchestration still chooses when a
database starts or stops; an engine owns the database-specific container,
connection, migration, and administrative-SQL details.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable
from urllib.parse import quote

from docker.client import DockerClient
from docker.errors import APIError, ContainerError, DockerException, NotFound
from docker.models.containers import Container
from docker.types import Mount
from requests.exceptions import ReadTimeout

from app.previews.config import PreviewSettings
from app.previews.models import PreviewConfiguration, PreviewDependencyService
from app.controller.store import ControllerStore
from app.sandboxes.engine_detection import NO_DATABASE
from app.sandboxes.naming import (
    database_name,
    db_data_volume,
    network as sandbox_network_name,
    ownership_labels,
    validate_ownership,
    workspace_volume,
)


ErrorFactory = Callable[[int, str], Exception]
MYSQL_PORT = 3306
POSTGRES_PORT = 5432
SHARED_DATABASE_PREFIX = "orchestrator-shared-db-"
# This path is deliberately not below /workspace.  The workspace is the Git
# clone in every runtime, while this mount always holds sandbox-owned data.
SQLITE_DATA_MOUNT_PATH = "/var/lib/orchestrator/sqlite"
SQLITE_DATABASE_PATH = f"{SQLITE_DATA_MOUNT_PATH}/database.sqlite3"
SQLITE_HELPER_IMAGE = "alpine:3.21"
DEFAULT_MYSQL_IMAGE = "mysql:8.4"
DEFAULT_POSTGRES_IMAGE = "postgres:17"
DEFAULT_MIGRATION_IMAGE = "node:22-bookworm-slim"


class SandboxDatabaseError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class SandboxMigrationError(SandboxDatabaseError):
    """The approved snapshot failed inside the restricted runtime."""


@dataclass(frozen=True)
class SandboxDatabaseRuntime:
    sandbox_id: str
    engine: str
    db_name: str
    database_url: str
    network_name: str
    data_volume: str | None

    @property
    def environment(self) -> dict[str, str]:
        return {"DATABASE_URL": self.database_url}

    @property
    def volumes(self) -> dict[str, dict[str, str]]:
        if self.data_volume is None:
            return {}
        return {
            self.data_volume: {
                "bind": SQLITE_DATA_MOUNT_PATH,
                "mode": "rw",
            }
        }


def sqlite_data_volume(sandbox_id: str) -> str:
    """Return the deterministic sandbox-owned SQLite data-volume name."""
    return db_data_volume(sandbox_id)


@dataclass(frozen=True)
class DatabaseProvisionRequest:
    docker_client: DockerClient
    image: str
    database: str
    container_name: str
    labels: dict[str, str]
    data_volume: str
    credentials_volume: Any
    network_name: str
    memory_limit: str
    nano_cpus: int
    pids_limit: int
    error: ErrorFactory
    shared: bool = False
    max_connections: int | None = None
    existing_container: Container | None = None


@dataclass(frozen=True)
class DatabaseProvision:
    container: Container | None
    credentials: dict[str, str]


@dataclass(frozen=True)
class DatabaseSchemaProvisionRequest:
    docker_client: DockerClient
    image: str
    network_name: str
    host: str
    credentials_volume: Any
    statements: list[str]
    error: ErrorFactory


@dataclass(frozen=True)
class DatabaseConnectionRequest:
    config: PreviewConfiguration
    database: PreviewDependencyService
    credentials: dict[str, str]
    error: ErrorFactory
    database_name: str = ""


@dataclass(frozen=True)
class DatabaseMigrationRequest:
    docker_client: DockerClient
    settings: PreviewSettings
    image: str
    commands: list[str]
    runtime: str
    environment: dict[str, str]
    volumes: dict[str, dict[str, str]]
    labels: dict[str, str]
    network: Any
    run_id: str
    error: ErrorFactory
    mounts: list[Mount] | None = None
    tmpfs: dict[str, str] | None = None
    container_name: str | None = None
    service_label: str = "initialize"


@dataclass(frozen=True)
class DatabaseDropRequest:
    docker_client: DockerClient
    image: str
    network_name: str
    host: str
    credentials_volume: Any
    statements: list[str]
    error: ErrorFactory
    data_volume: str = ""


ProvisionRequest = DatabaseProvisionRequest | DatabaseSchemaProvisionRequest


@runtime_checkable
class DatabaseEngine(Protocol):
    """The complete database-engine surface for this phase."""

    supports_template: bool

    def provision(self, request: ProvisionRequest) -> DatabaseProvision | None: ...

    def connection_url(self, request: DatabaseConnectionRequest) -> dict[str, str]: ...

    def run_migrations(self, request: DatabaseMigrationRequest) -> Container: ...

    def drop(self, request: DatabaseDropRequest) -> None: ...


class MySQLDatabaseEngine:
    """The MySQL preview behaviour that existed before the engine protocol."""

    supports_template = False

    def provision(self, request: ProvisionRequest) -> DatabaseProvision | None:
        if isinstance(request, DatabaseSchemaProvisionRequest):
            self._run_shared_sql(
                docker_client=request.docker_client,
                image=request.image,
                network_name=request.network_name,
                host=request.host,
                credentials_volume=request.credentials_volume,
                statements=request.statements,
                error=request.error,
            )
            return None
        credentials = self._read_or_create_credentials(
            request.docker_client,
            request.image,
            request.credentials_volume,
            error=request.error,
        )
        if request.existing_container is not None:
            return DatabaseProvision(
                container=request.existing_container,
                credentials=credentials,
            )
        environment = (
            {"MYSQL_ROOT_PASSWORD": credentials["root_password"]}
            if request.shared
            else {
                "MYSQL_DATABASE": request.database,
                "MYSQL_USER": credentials["username"],
                "MYSQL_PASSWORD": credentials["password"],
                "MYSQL_ROOT_PASSWORD": credentials["root_password"],
            }
        )
        command = (
            ["--max-connections", str(request.max_connections)]
            if request.shared and request.max_connections is not None
            else None
        )
        try:
            container = request.docker_client.containers.create(
                image=request.image,
                name=request.container_name,
                command=command,
                init=True,
                read_only=True,
                cap_drop=["ALL"],
                cap_add=["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"],
                security_opt=["no-new-privileges:true"],
                environment=environment,
                labels=request.labels,
                volumes={request.data_volume: {"bind": "/var/lib/mysql", "mode": "rw"}},
                tmpfs={
                    "/tmp": "rw,nosuid,size=256m",
                    "/var/run/mysqld": "rw,nosuid,size=32m,uid=999,gid=999",
                },
                network=request.network_name,
                ports=None,
                restart_policy={"Name": "no"},
                healthcheck={
                    "test": [
                        "CMD-SHELL",
                        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqladmin ping '
                        "-h 127.0.0.1 -u root --silent",
                    ],
                    "interval": 1_000_000_000,
                    "timeout": 3_000_000_000,
                    "retries": 60,
                    "start_period": 5_000_000_000,
                },
                mem_limit=request.memory_limit,
                nano_cpus=request.nano_cpus,
                pids_limit=request.pids_limit,
            )
        except APIError:
            # Another preview can create the project's shared server between
            # the caller's lookup and this create request.
            if not request.shared:
                raise
            container = request.docker_client.containers.get(request.container_name)
            container.reload()
        return DatabaseProvision(container=container, credentials=credentials)

    def connection_url(self, request: DatabaseConnectionRequest) -> dict[str, str]:
        environment: dict[str, str] = {}
        schema = request.database_name or request.database.database
        for variable, source in request.config.environment.items():
            if not source.from_service:
                continue
            if variable != "DATABASE_URL" or source.from_service != "database":
                raise request.error(
                    422,
                    "MySQL previews support only DATABASE_URL from the database service",
                )
            username = quote(request.credentials["username"], safe="")
            password = quote(request.credentials["password"], safe="")
            environment[variable] = (
                f"mysql://{username}:{password}@database:{MYSQL_PORT}/{schema}"
            )
        return environment

    def run_migrations(self, request: DatabaseMigrationRequest) -> Container:
        command = "set -eu\n"
        if request.runtime == "fastapi":
            command += ". /opt/venv/bin/activate\n"
        command += "\n".join(request.commands)
        container = request.docker_client.containers.create(
            image=request.image,
            command=["sh", "-lc", command],
            name=request.container_name
            or f"orchestrator-preview-{request.run_id[:12]}-initialize",
            init=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            working_dir="/workspace",
            environment=request.environment,
            labels={
                **request.labels,
                "orchestrator.preview.service": request.service_label,
            },
            volumes=request.volumes,
            mounts=request.mounts or None,
            tmpfs=request.tmpfs or {"/tmp": "rw,nosuid,size=256m"},
            network=request.network.name,
            ports=None,
            restart_policy={"Name": "no"},
            mem_limit=request.settings.preview_memory,
            nano_cpus=1_000_000_000,
            pids_limit=256,
        )
        container.start()
        try:
            result = container.wait(timeout=request.settings.prepare_timeout_seconds)
        except ReadTimeout as error:
            container.stop(timeout=2)
            raise request.error(
                408,
                "Database initialization exceeded "
                f"{request.settings.prepare_timeout_seconds} seconds",
            ) from error
        exit_code = int(result.get("StatusCode", 1))
        if exit_code != 0:
            logs = container.logs(stdout=True, stderr=True, tail=100)
            detail = (
                logs.decode("utf-8", errors="replace")
                if isinstance(logs, bytes)
                else str(logs)
            )[-8_192:]
            raise request.error(
                422,
                f"Database initialization failed with code {exit_code}: {detail}",
            )
        return container

    def drop(self, request: DatabaseDropRequest) -> None:
        """Runs administrative SQL as root outside application containers."""
        database_name_match = next(
            (
                re.search(r"DROP DATABASE IF EXISTS [`\"]([^`\"]+)[`\"]", statement)
                for statement in request.statements
                if "DROP DATABASE IF EXISTS" in statement
            ),
            None,
        )
        if database_name_match is not None:
            self._run_shared_drop(
                docker_client=request.docker_client,
                image=request.image,
                network_name=request.network_name,
                host=request.host,
                database_name=database_name_match.group(1),
                credentials_volume=request.credentials_volume,
                statements=request.statements,
                error=request.error,
            )
            return
        self._run_shared_sql(
            docker_client=request.docker_client,
            image=request.image,
            network_name=request.network_name,
            host=request.host,
            credentials_volume=request.credentials_volume,
            statements=request.statements,
            error=request.error,
        )

    def _run_shared_drop(
        self,
        *,
        docker_client: DockerClient,
        image: str,
        network_name: str,
        host: str,
        database_name: str,
        credentials_volume: Any,
        statements: list[str],
        error: ErrorFactory,
    ) -> None:
        """Kill sandbox sessions before the idempotent MySQL drop script."""
        root_password = self._read_or_create_credentials(
            docker_client,
            image,
            credentials_volume,
            create_missing=False,
            error=error,
        )["root_password"]
        script = "\n".join(f"{statement};" for statement in statements)
        try:
            docker_client.containers.run(
                image=image,
                command=[
                    "sh",
                    "-c",
                    "set -eu; "
                    "for id in $(mysql --protocol=TCP -h \"$PREVIEW_HOST\" -u root -Nse "
                    "\"SELECT ID FROM information_schema.PROCESSLIST "
                    "WHERE DB = '$PREVIEW_DATABASE' AND ID <> CONNECTION_ID()\"); do "
                    "mysql --protocol=TCP -h \"$PREVIEW_HOST\" -u root -e \"KILL $id\"; "
                    "done; printf '%s' \"$PREVIEW_SQL\" | "
                    "mysql --protocol=TCP -h \"$PREVIEW_HOST\" -u root",
                ],
                environment={
                    "PREVIEW_SQL": script,
                    "PREVIEW_HOST": host,
                    "PREVIEW_DATABASE": database_name,
                    "MYSQL_PWD": root_password,
                },
                remove=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network=network_name,
                tmpfs={"/tmp": "rw,nosuid,size=32m"},
            )
        except ContainerError as container_error:
            detail = container_error.stderr
            text = (
                detail.decode("utf-8", errors="replace")
                if isinstance(detail, bytes)
                else str(detail or "")
            )[-2_048:]
            raise error(500, f"Shared database statement failed: {text}") from container_error

    def _run_shared_sql(
        self,
        *,
        docker_client: DockerClient,
        image: str,
        network_name: str,
        host: str,
        credentials_volume: Any,
        statements: list[str],
        error: ErrorFactory,
    ) -> None:
        root_password = self._read_or_create_credentials(
            docker_client,
            image,
            credentials_volume,
            create_missing=False,
            error=error,
        )["root_password"]
        script = "\n".join(f"{statement};" for statement in statements)
        try:
            docker_client.containers.run(
                image=image,
                command=[
                    "sh",
                    "-c",
                    'set -eu; printf "%s" "$PREVIEW_SQL" | '
                    'mysql --protocol=TCP -h "$PREVIEW_HOST" -u root',
                ],
                environment={
                    "PREVIEW_SQL": script,
                    "PREVIEW_HOST": host,
                    "MYSQL_PWD": root_password,
                },
                remove=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network=network_name,
                tmpfs={"/tmp": "rw,nosuid,size=32m"},
            )
        except ContainerError as container_error:
            detail = container_error.stderr
            text = (
                detail.decode("utf-8", errors="replace")
                if isinstance(detail, bytes)
                else str(detail or "")
            )[-2_048:]
            raise error(500, f"Shared database statement failed: {text}") from container_error

    def _read_or_create_credentials(
        self,
        docker_client: DockerClient,
        image: str,
        credentials_volume: Any,
        *,
        create_missing: bool = True,
        error: ErrorFactory,
    ) -> dict[str, str]:
        """Reads MySQL credentials, generating them only on first provision."""
        output = docker_client.containers.run(
            image=image,
            command=[
                "sh",
                "-c",
                "if [ -f /credentials/mysql.json ]; then cat /credentials/mysql.json; fi",
            ],
            remove=True,
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            volumes={credentials_volume.name: {"bind": "/credentials", "mode": "ro"}},
        )
        if output:
            try:
                loaded = json.loads(
                    output.decode("utf-8") if isinstance(output, bytes) else str(output)
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise error(409, "Stored persistent database credentials are invalid") from exc
            if not isinstance(loaded, dict) or any(
                not isinstance(loaded.get(key), str) or not loaded[key]
                for key in ("username", "password", "root_password")
            ):
                raise error(409, "Stored persistent database credentials are invalid")
            return loaded

        if not create_missing:
            raise error(409, "Stored database credentials are missing")

        credentials = {
            "username": f"preview_{secrets.token_hex(4)}",
            "password": secrets.token_urlsafe(24),
            "root_password": secrets.token_urlsafe(32),
        }
        encoded = base64.b64encode(
            json.dumps(credentials, separators=(",", ":")).encode()
        ).decode("ascii")
        docker_client.containers.run(
            image=image,
            command=[
                "sh",
                "-c",
                (
                    "set -eu; umask 077; printf '%s' \"$DATABASE_CREDENTIALS\" | "
                    "base64 -d > /credentials/mysql.json"
                ),
            ],
            environment={"DATABASE_CREDENTIALS": encoded},
            remove=True,
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            volumes={credentials_volume.name: {"bind": "/credentials", "mode": "rw"}},
        )
        return credentials


class PostgreSQLDatabaseEngine(MySQLDatabaseEngine):
    """PostgreSQL server and administrative-SQL implementation.

    A shared server contains no sandbox data at creation time.  Callers create
    each sandbox role and database through ``DatabaseSchemaProvisionRequest``.
    This mirrors the existing MySQL shared-server split.
    """

    supports_template = True

    def provision(self, request: ProvisionRequest) -> DatabaseProvision | None:
        if isinstance(request, DatabaseSchemaProvisionRequest):
            self._run_shared_sql(
                docker_client=request.docker_client,
                image=request.image,
                network_name=request.network_name,
                host=request.host,
                credentials_volume=request.credentials_volume,
                statements=request.statements,
                error=request.error,
            )
            return None

        credentials = _read_or_create_server_credentials(
            request.docker_client,
            request.image,
            request.credentials_volume,
            filename="postgres.json",
            error=request.error,
        )
        if request.existing_container is not None:
            return DatabaseProvision(
                container=request.existing_container,
                credentials=credentials,
            )

        if request.shared:
            environment = {
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD": credentials["root_password"],
                "POSTGRES_DB": "postgres",
            }
        else:
            # A non-shared preview retains the existing one-server-per-preview
            # behaviour.  Its application role is the image's initial role.
            environment = {
                "POSTGRES_USER": credentials["username"],
                "POSTGRES_PASSWORD": credentials["password"],
                "POSTGRES_DB": request.database,
            }
        environment["POSTGRES_INITDB_ARGS"] = "--auth-host=scram-sha-256"
        try:
            container = request.docker_client.containers.create(
                image=request.image,
                name=request.container_name,
                init=True,
                read_only=True,
                cap_drop=["ALL"],
                cap_add=["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"],
                security_opt=["no-new-privileges:true"],
                environment=environment,
                labels=request.labels,
                volumes={request.data_volume: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
                tmpfs={"/tmp": "rw,nosuid,size=256m", "/var/run/postgresql": "rw,nosuid,size=32m"},
                network=request.network_name,
                ports=None,
                restart_policy={"Name": "no"},
                healthcheck={
                    "test": ["CMD-SHELL", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""],
                    "interval": 1_000_000_000,
                    "timeout": 3_000_000_000,
                    "retries": 60,
                    "start_period": 5_000_000_000,
                },
                mem_limit=request.memory_limit,
                nano_cpus=request.nano_cpus,
                pids_limit=request.pids_limit,
            )
        except APIError:
            if not request.shared:
                raise
            container = request.docker_client.containers.get(request.container_name)
            container.reload()
        return DatabaseProvision(container=container, credentials=credentials)

    def connection_url(self, request: DatabaseConnectionRequest) -> dict[str, str]:
        environment: dict[str, str] = {}
        database_name = request.database_name or request.database.database
        for variable, source in request.config.environment.items():
            if not source.from_service:
                continue
            if variable != "DATABASE_URL" or source.from_service != "database":
                raise request.error(
                    422,
                    "PostgreSQL previews support only DATABASE_URL from the database service",
                )
            username = quote(request.credentials["username"], safe="")
            password = quote(request.credentials["password"], safe="")
            environment[variable] = (
                f"postgres://{username}:{password}@database:{POSTGRES_PORT}/{database_name}"
            )
        return environment

    def drop(self, request: DatabaseDropRequest) -> None:
        """Runs PostgreSQL administrative SQL outside application containers."""
        self._run_shared_sql(
            docker_client=request.docker_client,
            image=request.image,
            network_name=request.network_name,
            host=request.host,
            credentials_volume=request.credentials_volume,
            statements=request.statements,
            error=request.error,
        )

    def _run_shared_sql(
        self,
        *,
        docker_client: DockerClient,
        image: str,
        network_name: str,
        host: str,
        credentials_volume: Any,
        statements: list[str],
        error: ErrorFactory,
    ) -> None:
        root_password = _read_or_create_server_credentials(
            docker_client,
            image,
            credentials_volume,
            filename="postgres.json",
            create_missing=False,
            error=error,
        )["root_password"]
        script = "\n".join(f"{statement};" for statement in statements)
        try:
            docker_client.containers.run(
                image=image,
                command=[
                    "sh",
                    "-c",
                    'set -eu; printf "%s" "$PREVIEW_SQL" | '
                    'psql -v ON_ERROR_STOP=1 -h "$PREVIEW_HOST" -U postgres -d postgres',
                ],
                environment={
                    "PREVIEW_SQL": script,
                    "PREVIEW_HOST": host,
                    "PGPASSWORD": root_password,
                },
                remove=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network=network_name,
                tmpfs={"/tmp": "rw,nosuid,size=32m"},
            )
        except ContainerError as container_error:
            detail = container_error.stderr
            text = (
                detail.decode("utf-8", errors="replace")
                if isinstance(detail, bytes)
                else str(detail or "")
            )[-2_048:]
            raise error(500, f"Shared database statement failed: {text}") from container_error


class SQLiteDatabaseEngine(MySQLDatabaseEngine):
    """SQLite data-file implementation with no database server or network."""

    supports_template = True

    def provision(self, request: ProvisionRequest) -> DatabaseProvision | None:
        if isinstance(request, DatabaseSchemaProvisionRequest):
            raise request.error(422, "SQLite does not support shared database schemas")
        _ensure_sqlite_volume(request)
        self._run_file_command(
            request.docker_client,
            request.data_volume,
            "set -eu; install -d -m 700 /database; : > /database/database.sqlite3; chmod 600 /database/database.sqlite3",
        )
        # SQLite has no server process and no credentials to hand to a runtime.
        return DatabaseProvision(container=None, credentials={})

    def connection_url(self, request: DatabaseConnectionRequest) -> dict[str, str]:
        environment: dict[str, str] = {}
        for variable, source in request.config.environment.items():
            if not source.from_service:
                continue
            if variable != "DATABASE_URL" or source.from_service != "database":
                raise request.error(
                    422,
                    "SQLite previews support only DATABASE_URL from the database service",
                )
            environment[variable] = f"file:{SQLITE_DATABASE_PATH}"
        return environment

    def run_migrations(self, request: DatabaseMigrationRequest) -> Container:
        command = "set -eu\n"
        if request.runtime == "fastapi":
            command += ". /opt/venv/bin/activate\n"
        command += "\n".join(request.commands)
        container = request.docker_client.containers.create(
            image=request.image,
            command=["sh", "-lc", command],
            name=request.container_name
            or f"orchestrator-preview-{request.run_id[:12]}-initialize",
            init=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            working_dir="/workspace",
            environment=request.environment,
            labels={
                **request.labels,
                "orchestrator.preview.service": request.service_label,
            },
            volumes=request.volumes,
            mounts=request.mounts or None,
            tmpfs=request.tmpfs or {"/tmp": "rw,nosuid,size=256m"},
            network_mode="none",
            ports=None,
            restart_policy={"Name": "no"},
            mem_limit=request.settings.preview_memory,
            nano_cpus=1_000_000_000,
            pids_limit=256,
        )
        container.start()
        try:
            result = container.wait(timeout=request.settings.prepare_timeout_seconds)
        except ReadTimeout as error:
            container.stop(timeout=2)
            raise request.error(
                408,
                "Database initialization exceeded "
                f"{request.settings.prepare_timeout_seconds} seconds",
            ) from error
        exit_code = int(result.get("StatusCode", 1))
        if exit_code != 0:
            logs = container.logs(stdout=True, stderr=True, tail=100)
            detail = (
                logs.decode("utf-8", errors="replace")
                if isinstance(logs, bytes)
                else str(logs)
            )[-8_192:]
            raise request.error(
                422,
                f"Database initialization failed with code {exit_code}: {detail}",
            )
        return container

    def drop(self, request: DatabaseDropRequest) -> None:
        if not request.data_volume:
            raise request.error(422, "SQLite drop requires the sandbox database volume")
        self._run_file_command(
            request.docker_client,
            request.data_volume,
            "set -eu; rm -f /database/database.sqlite3; : > /database/database.sqlite3; chmod 600 /database/database.sqlite3",
        )

    @staticmethod
    def _run_file_command(docker_client: DockerClient, volume_name: str, command: str) -> None:
        docker_client.containers.run(
            image=SQLITE_HELPER_IMAGE,
            command=["sh", "-c", command],
            remove=True,
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            volumes={volume_name: {"bind": "/database", "mode": "rw"}},
            tmpfs={"/tmp": "rw,nosuid,size=32m"},
        )


def _ensure_sqlite_volume(request: DatabaseProvisionRequest) -> Any:
    try:
        return request.docker_client.volumes.get(request.data_volume)
    except NotFound:
        pass
    try:
        return request.docker_client.volumes.create(
            name=request.data_volume,
            driver="local",
            labels=request.labels,
        )
    except APIError:
        return request.docker_client.volumes.get(request.data_volume)


def _read_or_create_server_credentials(
    docker_client: DockerClient,
    image: str,
    credentials_volume: Any,
    *,
    filename: str,
    create_missing: bool = True,
    error: ErrorFactory,
) -> dict[str, str]:
    """Read engine credentials without exposing a server root password to apps."""
    credential_path = f"/credentials/{filename}"
    output = docker_client.containers.run(
        image=image,
        command=["sh", "-c", f"if [ -f {credential_path} ]; then cat {credential_path}; fi"],
        remove=True,
        network_disabled=True,
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        volumes={credentials_volume.name: {"bind": "/credentials", "mode": "ro"}},
    )
    if output:
        try:
            loaded = json.loads(output.decode("utf-8") if isinstance(output, bytes) else str(output))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error(409, "Stored persistent database credentials are invalid") from exc
        if not isinstance(loaded, dict) or any(
            not isinstance(loaded.get(key), str) or not loaded[key]
            for key in ("username", "password", "root_password")
        ):
            raise error(409, "Stored persistent database credentials are invalid")
        return loaded
    if not create_missing:
        raise error(409, "Stored database credentials are missing")
    credentials = {
        "username": f"preview_{secrets.token_hex(4)}",
        "password": secrets.token_urlsafe(24),
        "root_password": secrets.token_urlsafe(32),
    }
    encoded = base64.b64encode(json.dumps(credentials, separators=(",", ":")).encode()).decode("ascii")
    docker_client.containers.run(
        image=image,
        command=[
            "sh",
            "-c",
            "set -eu; umask 077; printf '%s' \"$DATABASE_CREDENTIALS\" | base64 -d > /credentials/$DATABASE_CREDENTIAL_FILE",
        ],
        environment={"DATABASE_CREDENTIALS": encoded, "DATABASE_CREDENTIAL_FILE": filename},
        remove=True,
        network_disabled=True,
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        volumes={credentials_volume.name: {"bind": "/credentials", "mode": "rw"}},
    )
    return credentials


def mysql_shared_database_names(project_key: str) -> dict[str, str]:
    """Return stable shared-server resources for one project and engine.

    The name carries the engine, so a future engine change cannot reuse MySQL
    data or a server.
    """
    key = project_key[:12]
    prefix = f"{SHARED_DATABASE_PREFIX}{key}-mysql"
    return {
        "container": prefix,
        "data": f"{prefix}-data",
        "credentials": f"{prefix}-credentials",
        "network": f"{prefix}-net",
    }


def postgres_shared_database_names(project_key: str) -> dict[str, str]:
    """Return stable PostgreSQL shared-server resource names.

    The name carries the engine, matching `mysql_shared_database_names`.
    """
    key = project_key[:12]
    prefix = f"{SHARED_DATABASE_PREFIX}{key}-postgres"
    return {
        "container": prefix,
        "data": f"{prefix}-data",
        "credentials": f"{prefix}-credentials",
        "network": f"{prefix}-net",
    }


def shared_database_names(project_key: str, engine: str) -> dict[str, str]:
    """Resolve server resource names without assigning any SQLite resources."""
    if engine == "mysql":
        return mysql_shared_database_names(project_key)
    if engine == "postgres":
        return postgres_shared_database_names(project_key)
    raise ValueError(f"Database engine {engine!r} has no shared server")


def mysql_shared_schema_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{mysql_identifier(sandbox_id, error)}"


def mysql_shared_user_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{mysql_identifier(sandbox_id, error)}"


def mysql_identifier(sandbox_id: str, error: ErrorFactory) -> str:
    """Reduce a sandbox id to characters MySQL accepts unquoted."""
    cleaned = re.sub(r"[^a-z0-9]+", "", sandbox_id.casefold())[:16]
    if not cleaned:
        raise error(422, "Sandbox id cannot name a database schema")
    return cleaned


def postgres_identifier(sandbox_id: str, error: ErrorFactory) -> str:
    """Return the conservative unquoted identifier shared by role and database."""
    cleaned = re.sub(r"[^a-z0-9]+", "", sandbox_id.casefold())[:16]
    if not cleaned:
        raise error(422, "Sandbox id cannot name a PostgreSQL database")
    return cleaned


def postgres_shared_database_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{postgres_identifier(sandbox_id, error)}"


def postgres_shared_role_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{postgres_identifier(sandbox_id, error)}"


def postgres_provision_statements(
    sandbox_id: str,
    password: str,
    error: ErrorFactory,
) -> list[str]:
    """Build root-only SQL for one sandbox database and login role."""
    name = postgres_shared_database_name(sandbox_id, error)
    escaped_password = password.replace("'", "''")
    return [
        f'CREATE ROLE "{name}" LOGIN PASSWORD \'{escaped_password}\'',
        f'CREATE DATABASE "{name}" OWNER "{name}"',
    ]


def postgres_drop_statements(sandbox_id: str, error: ErrorFactory) -> list[str]:
    """Terminate clients before dropping the sandbox database and role."""
    name = postgres_shared_database_name(sandbox_id, error)
    escaped_name = name.replace("'", "''")
    return [
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{escaped_name}' AND pid <> pg_backend_pid()",
        f'DROP DATABASE IF EXISTS "{name}"',
        f'DROP ROLE IF EXISTS "{name}"',
    ]


def wait_for_mysql_health(
    container: Container,
    *,
    timeout_seconds: int,
    error: ErrorFactory,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        status = str(state.get("Status") or container.status)
        health = str((state.get("Health") or {}).get("Status") or "")
        if status == "running" and health == "healthy":
            return
        if status in {"dead", "exited"} or health == "unhealthy":
            logs = container.logs(stdout=True, stderr=True, tail=100)
            detail = (
                logs.decode("utf-8", errors="replace")
                if isinstance(logs, bytes)
                else str(logs)
            )[-8_192:]
            raise error(422, f"MySQL failed its health check: {detail}")
        time.sleep(0.5)
    raise error(408, f"MySQL health check exceeded {timeout_seconds} seconds")


def schema_baseline_hash(files: dict[str, bytes]) -> str:
    """Hash sorted path-and-byte pairs without parsing project content."""
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        encoded_path = path.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def sandbox_database_runtime(
    docker_client: DockerClient,
    store: ControllerStore,
    sandbox_id: str,
    *,
    require_ready: bool = True,
) -> SandboxDatabaseRuntime | None:
    """Resolve one managed sandbox's application-only database connection."""
    sandbox = store.sandbox(sandbox_id)
    if sandbox is None or sandbox.get("lifecycle_version") != "v1":
        return None
    if sandbox.get("db_engine") == NO_DATABASE:
        return None
    row = store.sandbox_database(sandbox_id)
    if row is None:
        if require_ready:
            raise SandboxDatabaseError(409, f"Sandbox '{sandbox_id}' has no database")
        return None
    if require_ready and str(row["status"]) != "ready":
        raise SandboxDatabaseError(
            409,
            f"Sandbox '{sandbox_id}' database is '{row['status']}'",
        )
    network_name = sandbox_network_name(sandbox_id)
    network = _owned_sandbox_network(
        docker_client,
        sandbox_id=sandbox_id,
        project_id=str(sandbox["project_id"]),
        create=False,
    )
    if network is None:
        raise SandboxDatabaseError(409, f"Sandbox database network '{network_name}' is missing")
    engine = str(row["engine"])
    data_volume = db_data_volume(sandbox_id) if engine == "sqlite" else None
    if data_volume is not None:
        try:
            volume = docker_client.volumes.get(data_volume)
        except NotFound as error:
            raise SandboxDatabaseError(
                409,
                f"Sandbox database volume '{data_volume}' is missing",
            ) from error
        try:
            validate_ownership(volume, sandbox_id=sandbox_id)
        except ValueError as error:
            raise SandboxDatabaseError(409, str(error)) from error
    return SandboxDatabaseRuntime(
        sandbox_id=sandbox_id,
        engine=engine,
        db_name=str(row["db_name"]),
        database_url=_sandbox_database_url(row),
        network_name=network_name,
        data_volume=data_volume,
    )


def provision_sandbox_database(
    docker_client: DockerClient,
    store: ControllerStore,
    settings: PreviewSettings,
    *,
    sandbox_id: str,
    migrate_commands: list[str],
    seed_commands: list[str],
    schema_files: dict[str, bytes] | None = None,
    rebuild: bool = False,
) -> tuple[SandboxDatabaseRuntime, str]:
    """Converge one sandbox database, then replay its approved snapshot."""
    sandbox = store.sandbox(sandbox_id)
    if sandbox is None or sandbox.get("lifecycle_version") != "v1":
        raise SandboxDatabaseError(409, "Only managed v1 sandboxes own databases")
    detection = store.sandbox_engine_detection(sandbox_id)
    engine_name = str((detection or {}).get("confirmed_engine") or "")
    if engine_name == NO_DATABASE:
        raise SandboxDatabaseError(409, f"Sandbox '{sandbox_id}' has no database")
    if engine_name not in DATABASE_ENGINES:
        raise SandboxDatabaseError(409, "Sandbox engine is not confirmed")
    db_name = database_name(sandbox_id)
    username = db_name
    row, created = store.ensure_sandbox_database(
        sandbox_id=sandbox_id,
        engine=engine_name,
        db_name=db_name,
        username=username,
        password=secrets.token_urlsafe(32),
    )
    if not rebuild and not created and str(row["status"]) == "ready":
        runtime = sandbox_database_runtime(docker_client, store, sandbox_id)
        if runtime is None:  # pragma: no cover - v1 checked above
            raise RuntimeError("sandbox database runtime disappeared")
        sandbox_row = store.sandbox(sandbox_id) or {}
        return runtime, str(sandbox_row.get("schema_baseline_hash") or schema_baseline_hash({}))

    store.update_sandbox_database_status(sandbox_id, status="provisioning")
    labels = {
        **ownership_labels(
            sandbox_id=sandbox_id,
            project_id=str(sandbox["project_id"]),
        ),
        "orchestrator.managed": "true",
        "orchestrator.kind": "sandbox-database",
    }
    network = _owned_sandbox_network(
        docker_client,
        sandbox_id=sandbox_id,
        project_id=str(sandbox["project_id"]),
        create=True,
    )
    if network is None:  # pragma: no cover - create=True
        raise RuntimeError("sandbox database network did not persist")
    store.record_sandbox_resource(sandbox_id, kind="network", name=network.name)

    engine = database_engine(engine_name, SandboxDatabaseError)
    data_volume: str | None = None
    shared: _SharedServer | None = None
    try:
        if engine_name == "sqlite":
            data_volume = db_data_volume(sandbox_id)
            _ensure_owned_sqlite_volume(
                docker_client,
                name=data_volume,
                labels=labels,
                sandbox_id=sandbox_id,
            )
            store.record_sandbox_resource(
                sandbox_id,
                kind="volume",
                name=data_volume,
            )
            if not created or rebuild:
                engine.drop(
                    DatabaseDropRequest(
                        docker_client=docker_client,
                        image=SQLITE_HELPER_IMAGE,
                        network_name=network.name,
                        host="",
                        credentials_volume=None,
                        statements=[],
                        error=SandboxDatabaseError,
                        data_volume=data_volume,
                    )
                )
            engine.provision(
                DatabaseProvisionRequest(
                    docker_client=docker_client,
                    image=SQLITE_HELPER_IMAGE,
                    database=db_name,
                    container_name=f"sbx-{sandbox_id[:12]}-sqlite",
                    labels=labels,
                    data_volume=data_volume,
                    credentials_volume=None,
                    network_name=network.name,
                    memory_limit=settings.preview_memory,
                    nano_cpus=1_000_000_000,
                    pids_limit=64,
                    error=SandboxDatabaseError,
                )
            )
        else:
            shared = _ensure_shared_server(
                docker_client,
                settings,
                project_id=str(sandbox["project_id"]),
                project_source=str(sandbox.get("project_name") or ""),
                engine_name=engine_name,
            )
            _connect_database_endpoint(network, shared.container)
            if created or rebuild or str(row["status"]) != "ready":
                engine.drop(
                    DatabaseDropRequest(
                        docker_client=docker_client,
                        image=shared.image,
                        network_name=shared.network.name,
                        host=shared.container.name,
                        credentials_volume=shared.credentials_volume,
                        statements=_drop_statements(engine_name, db_name),
                        error=SandboxDatabaseError,
                    )
                )
                engine.provision(
                    DatabaseSchemaProvisionRequest(
                        docker_client=docker_client,
                        image=shared.image,
                        network_name=shared.network.name,
                        host=shared.container.name,
                        credentials_volume=shared.credentials_volume,
                        statements=_provision_statements(
                            engine_name,
                            db_name,
                            str(row["password"]),
                        ),
                        error=SandboxDatabaseError,
                    )
                )

        runtime = SandboxDatabaseRuntime(
            sandbox_id=sandbox_id,
            engine=engine_name,
            db_name=db_name,
            database_url=_sandbox_database_url(row),
            network_name=network.name,
            data_volume=data_volume,
        )
        _run_sandbox_migrations(
            docker_client,
            settings,
            runtime=runtime,
            workspace=workspace_volume(sandbox_id),
            commands=[*migrate_commands, *seed_commands],
            labels=labels,
            network=network,
        )
        baseline_hash = schema_baseline_hash(schema_files or {})
        store.update_sandbox_database_status(
            sandbox_id,
            status="ready",
            provisioned=True,
        )
        return runtime, baseline_hash
    except Exception:
        store.update_sandbox_database_status(sandbox_id, status="failed")
        raise


def drop_sandbox_database(
    docker_client: DockerClient,
    store: ControllerStore,
    settings: PreviewSettings,
    *,
    sandbox_id: str,
) -> None:
    """Drop server-backed sandbox data before manifest-driven Docker cleanup."""
    sandbox = store.sandbox(sandbox_id)
    row = store.sandbox_database(sandbox_id)
    if (
        sandbox is None
        or sandbox.get("db_engine") == NO_DATABASE
        or row is None
        or str(row["engine"]) == "sqlite"
    ):
        return
    engine_name = str(row["engine"])
    shared = _ensure_shared_server(
        docker_client,
        settings,
        project_id=str(sandbox["project_id"]),
        project_source=str(sandbox.get("project_name") or ""),
        engine_name=engine_name,
    )
    database_engine(engine_name, SandboxDatabaseError).drop(
        DatabaseDropRequest(
            docker_client=docker_client,
            image=shared.image,
            network_name=shared.network.name,
            host=shared.container.name,
            credentials_volume=shared.credentials_volume,
            statements=_drop_statements(engine_name, str(row["db_name"])),
            error=SandboxDatabaseError,
        )
    )
    try:
        docker_client.networks.get(sandbox_network_name(sandbox_id)).disconnect(
            shared.container,
            force=True,
        )
    except (NotFound, APIError):
        pass
    store.update_sandbox_database_status(sandbox_id, status="dropped")


@dataclass(frozen=True)
class _SharedServer:
    image: str
    container: Container
    credentials_volume: Any
    network: Any


def _owned_sandbox_network(
    docker_client: DockerClient,
    *,
    sandbox_id: str,
    project_id: str,
    create: bool,
) -> Any | None:
    name = sandbox_network_name(sandbox_id)
    existing = [item for item in docker_client.networks.list(names=[name]) if item.name == name]
    if existing:
        try:
            validate_ownership(existing[0], sandbox_id=sandbox_id)
        except ValueError as error:
            raise SandboxDatabaseError(409, str(error)) from error
        return existing[0]
    if not create:
        return None
    labels = {
        **ownership_labels(sandbox_id=sandbox_id, project_id=project_id),
        "orchestrator.managed": "true",
        "orchestrator.kind": "sandbox-network",
    }
    try:
        return docker_client.networks.create(
            name,
            driver="bridge",
            internal=True,
            labels=labels,
        )
    except APIError:
        for item in docker_client.networks.list(names=[name]):
            if item.name == name:
                validate_ownership(item, sandbox_id=sandbox_id)
                return item
        raise


def _ensure_owned_sqlite_volume(
    docker_client: DockerClient,
    *,
    name: str,
    labels: dict[str, str],
    sandbox_id: str,
) -> Any:
    try:
        volume = docker_client.volumes.get(name)
    except NotFound:
        try:
            volume = docker_client.volumes.create(
                name=name,
                driver="local",
                labels=labels,
            )
        except APIError:
            volume = docker_client.volumes.get(name)
    try:
        validate_ownership(volume, sandbox_id=sandbox_id)
    except ValueError as error:
        raise SandboxDatabaseError(409, str(error)) from error
    return volume


def _ensure_shared_server(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    project_id: str,
    project_source: str,
    engine_name: str,
) -> _SharedServer:
    image = _database_image(engine_name)
    _ensure_image(docker_client, image)
    names = shared_database_names(project_id, engine_name)
    labels = {
        "orchestrator.managed": "true",
        "orchestrator.kind": "shared-database",
        "orchestrator.shared-database": "true",
        "orchestrator.shared-database.image": image,
        "orchestrator.project.id": project_id,
        "orchestrator.project.source": project_source,
        "orchestrator.preview.service": "database",
        "orchestrator.preview.persistent": "true",
    }
    network = _shared_resource(
        docker_client.networks,
        names["network"],
        lambda: docker_client.networks.create(
            names["network"], driver="bridge", internal=True, labels=labels
        ),
    )
    data_volume = _shared_resource(
        docker_client.volumes,
        names["data"],
        lambda: docker_client.volumes.create(
            name=names["data"], driver="local", labels=labels
        ),
    )
    credentials_volume = _shared_resource(
        docker_client.volumes,
        names["credentials"],
        lambda: docker_client.volumes.create(
            name=names["credentials"], driver="local", labels=labels
        ),
    )
    try:
        container = docker_client.containers.get(names["container"])
        container.reload()
    except NotFound:
        container = None
    provision = database_engine(engine_name, SandboxDatabaseError).provision(
        DatabaseProvisionRequest(
            docker_client=docker_client,
            image=image,
            database="",
            container_name=names["container"],
            labels=labels,
            data_volume=data_volume.name,
            credentials_volume=credentials_volume,
            network_name=network.name,
            memory_limit=settings.shared_database_memory,
            nano_cpus=2_000_000_000,
            pids_limit=512,
            error=SandboxDatabaseError,
            shared=True,
            max_connections=settings.shared_database_max_connections,
            existing_container=container,
        )
    )
    if provision is None or provision.container is None:
        raise RuntimeError("shared database server provisioning returned no container")
    container = provision.container
    if container.status != "running":
        container.start()
    _wait_for_server_health(
        container,
        engine_name=engine_name,
        timeout_seconds=settings.prepare_timeout_seconds,
    )
    return _SharedServer(image, container, credentials_volume, network)


def _shared_resource(collection: Any, name: str, create: Callable[[], Any]) -> Any:
    try:
        return collection.get(name)
    except NotFound:
        pass
    try:
        return create()
    except APIError:
        return collection.get(name)


def _connect_database_endpoint(network: Any, container: Container) -> None:
    try:
        network.connect(container, aliases=["database"])
    except APIError as error:
        message = str(error).casefold()
        if "already exists" not in message and "already connected" not in message:
            raise


def _run_sandbox_migrations(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    runtime: SandboxDatabaseRuntime,
    workspace: str,
    commands: list[str],
    labels: dict[str, str],
    network: Any,
) -> None:
    if not commands:
        return
    image = os.getenv("SANDBOX_MIGRATION_IMAGE", DEFAULT_MIGRATION_IMAGE)
    _ensure_image(docker_client, image)
    runner: Container | None = None
    try:
        runner = database_engine(runtime.engine, SandboxDatabaseError).run_migrations(
            DatabaseMigrationRequest(
                docker_client=docker_client,
                settings=settings,
                image=image,
                commands=commands,
                runtime="sandbox",
                environment=runtime.environment,
                volumes={
                    workspace: {"bind": "/workspace", "mode": "rw"},
                    **runtime.volumes,
                },
                labels=labels,
                network=network,
                run_id=runtime.sandbox_id,
                error=SandboxDatabaseError,
                tmpfs={"/tmp": "rw,nosuid,size=256m"},
                container_name=f"sbx-{runtime.sandbox_id[:12]}-database-migrate",
                service_label="sandbox-migration",
            )
        )
    except SandboxDatabaseError as error:
        raise SandboxMigrationError(error.status_code, error.detail) from error
    except DockerException as error:
        raise SandboxMigrationError(
            503,
            f"Database migration runtime failed: {error}",
        ) from error
    finally:
        if runner is not None:
            try:
                runner.remove(force=True)
            except DockerException:
                pass


def _database_image(engine_name: str) -> str:
    if engine_name == "mysql":
        return os.getenv("SANDBOX_MYSQL_IMAGE", DEFAULT_MYSQL_IMAGE)
    if engine_name == "postgres":
        return os.getenv("SANDBOX_POSTGRES_IMAGE", DEFAULT_POSTGRES_IMAGE)
    raise SandboxDatabaseError(422, f"Engine '{engine_name}' has no server image")


def _ensure_image(docker_client: DockerClient, image: str) -> None:
    try:
        docker_client.images.get(image)
    except NotFound:
        docker_client.images.pull(image)


def _wait_for_server_health(
    container: Container,
    *,
    engine_name: str,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        status = str(state.get("Status") or container.status)
        health_state = state.get("Health")
        health = str((health_state or {}).get("Status") or "")
        if status == "running" and (health == "healthy" or health_state is None):
            return
        if status in {"dead", "exited"} or health == "unhealthy":
            raise SandboxDatabaseError(422, f"{engine_name} database server is unhealthy")
        time.sleep(0.5)
    raise SandboxDatabaseError(
        408,
        f"{engine_name} database health check exceeded {timeout_seconds} seconds",
    )


def _sandbox_database_url(row: dict[str, Any]) -> str:
    engine = str(row["engine"])
    if engine == "sqlite":
        return f"file:{SQLITE_DATABASE_PATH}"
    scheme = "postgres" if engine == "postgres" else "mysql"
    username = quote(str(row["username"]), safe="")
    password = quote(str(row["password"]), safe="")
    return f"{scheme}://{username}:{password}@database/{row['db_name']}"


def _drop_statements(engine_name: str, db_name: str) -> list[str]:
    if engine_name == "postgres":
        escaped = db_name.replace("'", "''")
        return [
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{escaped}' AND pid <> pg_backend_pid()",
            f'DROP DATABASE IF EXISTS "{db_name}"',
            f'DROP ROLE IF EXISTS "{db_name}"',
        ]
    return [
        f"DROP DATABASE IF EXISTS `{db_name}`",
        f"DROP USER IF EXISTS '{db_name}'@'%'",
    ]


def _provision_statements(
    engine_name: str,
    db_name: str,
    password: str,
) -> list[str]:
    escaped_password = password.replace("'", "''")
    if engine_name == "postgres":
        return [
            f'CREATE ROLE "{db_name}" LOGIN PASSWORD \'{escaped_password}\'',
            f'CREATE DATABASE "{db_name}" OWNER "{db_name}"',
        ]
    return [
        f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        f"CREATE USER '{db_name}'@'%' IDENTIFIED BY '{escaped_password}'",
        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_name}'@'%'",
        "FLUSH PRIVILEGES",
    ]


MYSQL_DATABASE: DatabaseEngine = MySQLDatabaseEngine()
POSTGRESQL_DATABASE: DatabaseEngine = PostgreSQLDatabaseEngine()
# Keep the shorter spelling available to callers that use the engine key.
POSTGRES_DATABASE = POSTGRESQL_DATABASE
SQLITE_DATABASE: DatabaseEngine = SQLiteDatabaseEngine()
DATABASE_ENGINES: dict[str, DatabaseEngine] = {
    "mysql": MYSQL_DATABASE,
    "postgres": POSTGRESQL_DATABASE,
    "sqlite": SQLITE_DATABASE,
}


def database_engine(engine: str, error: ErrorFactory) -> DatabaseEngine:
    """Resolve a confirmed engine name to its protocol implementation."""
    try:
        return DATABASE_ENGINES[engine]
    except KeyError as exc:
        raise error(422, f"Unsupported database engine: {engine}") from exc
