"""MySQL database engine."""

import re
from typing import Any
from urllib.parse import quote

from docker.client import DockerClient
from docker.errors import APIError, ContainerError
from docker.models.containers import Container
from requests.exceptions import ReadTimeout

from app.containers.hardened import Capabilities, HardenedContainerSpec, create_hardened
from app.platform.labels import LABEL_SERVICE

from ._engine_ops import _read_or_create_server_credentials, _run_database_command
from .constants import MYSQL_PORT
from .contracts import (
    DatabaseConnectionRequest,
    DatabaseDropRequest,
    DatabaseMigrationRequest,
    DatabaseProvision,
    DatabaseSchemaProvisionRequest,
    ErrorFactory,
    ProvisionRequest,
)


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
            container = create_hardened(
                request.docker_client,
                HardenedContainerSpec(
                    image=request.image,
                    name=request.container_name,
                    command=command,
                    capabilities=Capabilities.DATABASE_SERVER,
                    environment=environment,
                    labels=request.labels,
                    volumes={
                        request.data_volume: {"bind": "/var/lib/mysql", "mode": "rw"}
                    },
                    tmpfs_size="256m",
                    extra_tmpfs={
                        "/var/run/mysqld": "rw,nosuid,size=32m,uid=999,gid=999"
                    },
                    network=request.network_name,
                    restart_policy={"Name": "no"},
                    healthcheck={
                        "test": [
                            "CMD-SHELL",
                            (
                                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqladmin ping '
                                "-h 127.0.0.1 -u root --silent"
                            ),
                        ],
                        "interval": 1_000_000_000,
                        "timeout": 3_000_000_000,
                        "retries": 60,
                        "start_period": 5_000_000_000,
                    },
                    mem_limit=request.memory_limit,
                    nano_cpus=request.nano_cpus,
                    pids_limit=request.pids_limit,
                ),
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
        container = create_hardened(
            request.docker_client,
            HardenedContainerSpec(
                image=request.image,
                command=["sh", "-lc", command],
                name=request.container_name
                or f"orchestrator-preview-{request.run_id[:12]}-initialize",
                working_dir="/workspace",
                environment=request.environment,
                labels={
                    **request.labels,
                    LABEL_SERVICE: request.service_label,
                },
                volumes=request.volumes,
                mounts=request.mounts or None,
                tmpfs_size="256m",
                extra_tmpfs={
                    key: value
                    for key, value in (request.tmpfs or {}).items()
                    if key != "/tmp"
                },
                network=request.network.name,
                restart_policy={"Name": "no"},
                mem_limit=request.settings.preview_memory,
                nano_cpus=1_000_000_000,
                pids_limit=256,
            ),
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
            _run_database_command(
                docker_client,
                image=image,
                command=[
                    "sh",
                    "-c",
                    (
                        "set -eu; "
                        'for id in $(mysql --protocol=TCP -h "$PREVIEW_HOST" -u root -Nse '
                        '"SELECT ID FROM information_schema.PROCESSLIST '
                        "WHERE DB = '$PREVIEW_DATABASE' AND ID <> CONNECTION_ID()\"); do "
                        'mysql --protocol=TCP -h "$PREVIEW_HOST" -u root -e "KILL $id"; '
                        "done; printf '%s' \"$PREVIEW_SQL\" | "
                        'mysql --protocol=TCP -h "$PREVIEW_HOST" -u root'
                    ),
                ],
                environment={
                    "PREVIEW_SQL": script,
                    "PREVIEW_HOST": host,
                    "PREVIEW_DATABASE": database_name,
                    "MYSQL_PWD": root_password,
                },
                network=network_name,
                tmpfs_size="32m",
            )
        except ContainerError as container_error:
            detail = container_error.stderr
            text = (
                detail.decode("utf-8", errors="replace")
                if isinstance(detail, bytes)
                else str(detail or "")
            )[-2_048:]
            raise error(
                500, f"Shared database statement failed: {text}"
            ) from container_error

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
            _run_database_command(
                docker_client,
                image=image,
                command=[
                    "sh",
                    "-c",
                    (
                        'set -eu; printf "%s" "$PREVIEW_SQL" | '
                        'mysql --protocol=TCP -h "$PREVIEW_HOST" -u root'
                    ),
                ],
                environment={
                    "PREVIEW_SQL": script,
                    "PREVIEW_HOST": host,
                    "MYSQL_PWD": root_password,
                },
                network=network_name,
                tmpfs_size="32m",
            )
        except ContainerError as container_error:
            detail = container_error.stderr
            text = (
                detail.decode("utf-8", errors="replace")
                if isinstance(detail, bytes)
                else str(detail or "")
            )[-2_048:]
            raise error(
                500, f"Shared database statement failed: {text}"
            ) from container_error

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
        return _read_or_create_server_credentials(
            docker_client,
            image,
            credentials_volume,
            filename="mysql.json",
            create_missing=create_missing,
            error=error,
        )
