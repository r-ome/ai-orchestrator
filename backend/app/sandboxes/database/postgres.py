"""PostgreSQL database engine."""

from typing import Any
from urllib.parse import quote

from docker.client import DockerClient
from docker.errors import APIError, ContainerError

from app.containers.hardened import Capabilities, HardenedContainerSpec, create_hardened

from ._engine_ops import _read_or_create_server_credentials, _run_database_command
from .constants import POSTGRES_PORT
from .contracts import (
    DatabaseConnectionRequest,
    DatabaseDropRequest,
    DatabaseProvision,
    DatabaseSchemaProvisionRequest,
    ErrorFactory,
    ProvisionRequest,
)
from .mysql import MySQLDatabaseEngine


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
            container = create_hardened(
                request.docker_client,
                HardenedContainerSpec(
                    image=request.image,
                    name=request.container_name,
                    capabilities=Capabilities.DATABASE_SERVER,
                    environment=environment,
                    labels=request.labels,
                    volumes={
                        request.data_volume: {
                            "bind": "/var/lib/postgresql/data",
                            "mode": "rw",
                        }
                    },
                    tmpfs_size="256m",
                    extra_tmpfs={"/var/run/postgresql": "rw,nosuid,size=32m"},
                    network=request.network_name,
                    restart_policy={"Name": "no"},
                    healthcheck={
                        "test": [
                            "CMD-SHELL",
                            'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
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
            _run_database_command(
                docker_client,
                image=image,
                command=[
                    "sh",
                    "-c",
                    (
                        'set -eu; printf "%s" "$PREVIEW_SQL" | '
                        'psql -v ON_ERROR_STOP=1 -h "$PREVIEW_HOST" -U postgres -d postgres'
                    ),
                ],
                environment={
                    "PREVIEW_SQL": script,
                    "PREVIEW_HOST": host,
                    "PGPASSWORD": root_password,
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
