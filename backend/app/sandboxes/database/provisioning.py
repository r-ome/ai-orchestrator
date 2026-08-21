"""Sandbox database provisioning."""

import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from docker.client import DockerClient
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from app.containers.config import PreviewRuntimeLimits
from app.containers.images import ensure_image
from app.controller.store import ControllerStore
from app.platform.labels import (
    LABEL_CONTROLLER_MANAGED,
    LABEL_KIND,
    LABEL_PERSISTENT,
    LABEL_PROJECT_ID,
    LABEL_PROJECT_SOURCE,
    LABEL_SERVICE,
    LABEL_SHARED_DATABASE,
    LABEL_SHARED_DATABASE_IMAGE,
)
from app.platform.naming import (
    database_name,
    db_data_volume,
    ownership_labels,
    validate_ownership,
    workspace_volume,
)
from app.platform.naming import (
    network as sandbox_network_name,
)
from app.sandboxes.engine_detection import NO_DATABASE

from .constants import (
    DEFAULT_MIGRATION_IMAGE,
    DEFAULT_MYSQL_IMAGE,
    DEFAULT_POSTGRES_IMAGE,
    SQLITE_DATABASE_PATH,
    SQLITE_HELPER_IMAGE,
)
from .contracts import (
    DatabaseDropRequest,
    DatabaseMigrationRequest,
    DatabaseProvisionRequest,
    DatabaseSchemaProvisionRequest,
    SandboxDatabaseRuntime,
)
from .errors import SandboxDatabaseError, SandboxMigrationError
from .registry import DATABASE_ENGINES, database_engine
from .shared import (
    schema_baseline_hash,
    shared_database_names,
    shared_database_server_lock,
)


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
        raise SandboxDatabaseError(
            409, f"Sandbox database network '{network_name}' is missing"
        )
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
    settings: PreviewRuntimeLimits,
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
        return runtime, str(
            sandbox_row.get("schema_baseline_hash") or schema_baseline_hash({})
        )

    store.update_sandbox_database_status(sandbox_id, status="provisioning")
    labels = {
        **ownership_labels(
            sandbox_id=sandbox_id,
            project_id=str(sandbox["project_id"]),
        ),
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_KIND: "sandbox-database",
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
                    memory_limit=settings.memory,
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
    settings: PreviewRuntimeLimits,
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
    existing = [
        item for item in docker_client.networks.list(names=[name]) if item.name == name
    ]
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
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_KIND: "sandbox-network",
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
    settings: PreviewRuntimeLimits,
    *,
    project_id: str,
    project_source: str,
    engine_name: str,
) -> _SharedServer:
    image = _database_image(engine_name)
    names = shared_database_names(project_id, engine_name)
    labels = {
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_KIND: "shared-database",
        LABEL_SHARED_DATABASE: "true",
        LABEL_SHARED_DATABASE_IMAGE: image,
        LABEL_PROJECT_ID: project_id,
        LABEL_PROJECT_SOURCE: project_source,
        LABEL_SERVICE: "database",
        LABEL_PERSISTENT: "true",
    }
    with shared_database_server_lock(names["container"]):
        ensure_image(docker_client, image)
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
        if container is not None:
            stored_image = (
                (container.attrs.get("Config") or {}).get("Labels") or {}
            ).get(LABEL_SHARED_DATABASE_IMAGE, "")
            if stored_image and stored_image != image:
                raise SandboxDatabaseError(
                    409,
                    "This project's shared database runs "
                    f"{stored_image}; the sandbox asks for {image}",
                )
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
            raise RuntimeError(
                "shared database server provisioning returned no container"
            )
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
    settings: PreviewRuntimeLimits,
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
    ensure_image(docker_client, image)
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
            raise SandboxDatabaseError(
                422, f"{engine_name} database server is unhealthy"
            )
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
            (
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{escaped}' AND pid <> pg_backend_pid()"
            ),
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
            f"CREATE ROLE \"{db_name}\" LOGIN PASSWORD '{escaped_password}'",
            f'CREATE DATABASE "{db_name}" OWNER "{db_name}"',
        ]
    return [
        f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        f"CREATE USER '{db_name}'@'%' IDENTIFIED BY '{escaped_password}'",
        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_name}'@'%'",
        "FLUSH PRIVILEGES",
    ]
