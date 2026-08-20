from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from docker.client import DockerClient
from docker.models.containers import Container
from docker.types import Mount

from app.platform.naming import db_data_volume
from app.previews.config import PreviewSettings
from app.previews.models import PreviewConfiguration, PreviewDependencyService

from .constants import SQLITE_DATA_MOUNT_PATH

ErrorFactory = Callable[[int, str], Exception]


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
