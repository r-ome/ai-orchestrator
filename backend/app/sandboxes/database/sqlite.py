"""SQLite database engine."""

from docker.client import DockerClient
from docker.models.containers import Container
from requests.exceptions import ReadTimeout

from app.containers.hardened import HardenedContainerSpec, create_hardened
from app.platform.labels import LABEL_SERVICE

from ._engine_ops import _ensure_sqlite_volume, _run_database_command
from .constants import SQLITE_DATABASE_PATH, SQLITE_HELPER_IMAGE
from .contracts import (
    DatabaseConnectionRequest,
    DatabaseDropRequest,
    DatabaseMigrationRequest,
    DatabaseProvision,
    DatabaseSchemaProvisionRequest,
    ProvisionRequest,
)
from .mysql import MySQLDatabaseEngine


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
                restart_policy={"Name": "no"},
                mem_limit=request.settings.memory,
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
        if not request.data_volume:
            raise request.error(422, "SQLite drop requires the sandbox database volume")
        self._run_file_command(
            request.docker_client,
            request.data_volume,
            "set -eu; rm -f /database/database.sqlite3; : > /database/database.sqlite3; chmod 600 /database/database.sqlite3",
        )

    @staticmethod
    def _run_file_command(
        docker_client: DockerClient, volume_name: str, command: str
    ) -> None:
        _run_database_command(
            docker_client,
            image=SQLITE_HELPER_IMAGE,
            command=["sh", "-c", command],
            volumes={volume_name: {"bind": "/database", "mode": "rw"}},
            tmpfs_size="32m",
        )
