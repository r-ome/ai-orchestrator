import re
import secrets
from typing import Any

from docker.client import DockerClient
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from app.controller.store import ControllerStore
from app.labels import (
    LABEL_CONTROLLER_MANAGED,
    LABEL_DATA_MANAGED,
    LABEL_KIND,
    LABEL_PERSISTENT,
    LABEL_PROJECT_ID,
    LABEL_PROJECT_SOURCE,
    LABEL_SERVICE,
    LABEL_SHARED_DATABASE,
    LABEL_SHARED_DATABASE_IMAGE,
)
from app.previews._shared import _ready_project
from app.previews.config import PreviewSettings
from app.previews.errors import PreviewOperationError
from app.previews.health import _wait_for_mysql_health
from app.previews.models import (
    DatabaseSharingState,
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewMode,
    PreviewPersistence,
    PreviewSharing,
    ProjectDatabaseSharing,
)
from app.previews.progress import ProgressReporter, _ignore_progress
from app.previews.resources import (
    _ensure_preview_image,
    _existing_volume,
    _preview_networks,
)
from app.sandboxes.database import (
    MYSQL_DATABASE,
    DatabaseDropRequest,
    DatabaseEngine,
    DatabaseProvisionRequest,
    DatabaseSchemaProvisionRequest,
    SandboxDatabaseError,
    SandboxDatabaseRuntime,
    mysql_identifier,
    mysql_shared_database_names,
    mysql_shared_schema_name,
    mysql_shared_user_name,
    sandbox_database_runtime,
    shared_database_server_lock,
)


# Raised at approval and again at attach, so one wording covers both.
_SHARED_DATA_UNAVAILABLE = (
    "shared_data is unavailable; each managed sandbox owns its database"
)
_database_engine: DatabaseEngine = MYSQL_DATABASE


def _shared_database_names(project_key: str) -> dict[str, str]:
    return mysql_shared_database_names(project_key)


def _shared_schema_name(sandbox_id: str) -> str:
    return mysql_shared_schema_name(sandbox_id, PreviewOperationError)


def _shared_user_name(sandbox_id: str) -> str:
    return mysql_shared_user_name(sandbox_id, PreviewOperationError)


def _identifier(sandbox_id: str) -> str:
    return mysql_identifier(sandbox_id, PreviewOperationError)


def _shared_database_labels(
    project_key: str,
    source_path: str,
    image: str,
) -> dict[str, str]:
    """Labels a shared server and its volumes.

    Deliberately carries no run id and no sandbox id. Every teardown path
    filters on those, so the shared server cannot be swept away with the run
    that happened to create it.
    """
    return {
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_KIND: "shared-database",
        LABEL_SHARED_DATABASE: "true",
        LABEL_SHARED_DATABASE_IMAGE: image,
        LABEL_PROJECT_ID: project_key,
        LABEL_PROJECT_SOURCE: source_path,
        LABEL_SERVICE: "database",
        LABEL_PERSISTENT: "true",
    }


def _shared_volume(docker_client: DockerClient, name: str, labels: dict[str, str]) -> Any:
    try:
        return docker_client.volumes.get(name)
    except NotFound:
        pass
    try:
        return docker_client.volumes.create(name=name, driver="local", labels=labels)
    except APIError:
        return docker_client.volumes.get(name)


def _shared_network(docker_client: DockerClient, name: str, labels: dict[str, str]) -> Any:
    existing = docker_client.networks.list(names=[name])
    for network in existing:
        if network.name == name:
            return network
    try:
        return docker_client.networks.create(
            name,
            driver="bridge",
            internal=True,
            labels=labels,
        )
    except APIError:
        for network in docker_client.networks.list(names=[name]):
            if network.name == name:
                return network
        raise


def _shared_database_server(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    project_key: str,
    source_path: str,
    database: PreviewDependencyService,
    report: ProgressReporter,
) -> tuple[Container, Any, Any]:
    """Returns the project's shared MySQL server, creating it on first use."""
    names = _shared_database_names(project_key)
    labels = _shared_database_labels(project_key, source_path, database.image)
    with shared_database_server_lock(names["container"]):
        report("database-image", f"Checking database image {database.image}")
        _ensure_preview_image(docker_client, database.image)
        network = _shared_network(docker_client, names["network"], labels)
        data_volume = _shared_volume(
            docker_client,
            names["data"],
            {**labels, LABEL_DATA_MANAGED: "true"},
        )
        credentials_volume = _shared_volume(
            docker_client,
            names["credentials"],
            {**labels, LABEL_DATA_MANAGED: "true", LABEL_SERVICE: "database-credentials"},
        )
        container = _existing_shared_server(docker_client, names["container"])
        created = container is None
        if created:
            report("database", "Creating the shared project database")
        provision = _database_engine.provision(
            DatabaseProvisionRequest(
                docker_client=docker_client,
                image=database.image,
                database=database.database,
                container_name=names["container"],
                labels=labels,
                data_volume=data_volume.name,
                credentials_volume=credentials_volume,
                network_name=network.name,
                memory_limit=settings.shared_database_memory,
                nano_cpus=2_000_000_000,
                pids_limit=512,
                error=PreviewOperationError,
                shared=True,
                max_connections=settings.shared_database_max_connections,
                existing_container=container,
            )
        )
        if provision is None:
            raise RuntimeError("MySQL container provisioning returned no container")
        container = provision.container
        if not created:
            stored_image = (
                (container.attrs.get("Config") or {}).get("Labels") or {}
            ).get(LABEL_SHARED_DATABASE_IMAGE, "")
            if stored_image and stored_image != database.image:
                raise PreviewOperationError(
                    409,
                    "This project's shared database runs "
                    f"{stored_image}; the proposal asks for {database.image}",
                )
            if container.status != "running":
                report("database", "Starting the shared project database")
                container.start()
        else:
            container.start()

        report("database-health", "Waiting for the shared database health check")
        _wait_for_mysql_health(
            container,
            timeout_seconds=settings.prepare_timeout_seconds,
        )
        report("database-health", "Shared database is healthy")
    return container, credentials_volume, network


def _existing_shared_server(
    docker_client: DockerClient,
    name: str,
) -> Container | None:
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        return None
    container.reload()
    return container


def _attach_shared_database(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    *,
    sandbox_id: str,
    project_key: str,
    source_path: str,
    database: PreviewDependencyService,
    run_network: Any,
    report: ProgressReporter,
) -> tuple[dict[str, str], str]:
    """Gives one sandbox credentials on the project's shared server.

    Returns the credentials and the schema the sandbox must use. Every managed
    sandbox owns its schema, so the schema is always this sandbox's own.
    """
    if database.sharing is PreviewSharing.SHARED_DATA:
        # `_validate_sharing` refuses shared_data at approval. An approval
        # recorded before that guard can still reach this call, so refuse the
        # guest here too rather than provision one nothing else supports.
        raise PreviewOperationError(422, _SHARED_DATA_UNAVAILABLE)
    server, credentials_volume, shared_network = _shared_database_server(
        docker_client,
        settings,
        project_key=project_key,
        source_path=source_path,
        database=database,
        report=report,
    )

    schema_name = _shared_schema_name(sandbox_id)
    # Schema names are truncated sandbox ids. A collision would silently join
    # two sandboxes' data, so refuse rather than share by accident.
    for row in controller_store.shared_schemas_for_project(project_key):
        if (
            str(row["schema_name"]) == schema_name
            and str(row["owner_sandbox_id"]) != sandbox_id
        ):
            raise PreviewOperationError(
                409,
                f"Schema name {schema_name} already belongs to another sandbox",
            )
    user_name = _shared_user_name(sandbox_id)
    password = secrets.token_urlsafe(24)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", password):
        raise PreviewOperationError(500, "Generated database password is unusable")

    statements = [
        f"CREATE DATABASE IF NOT EXISTS `{schema_name}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        f"CREATE USER IF NOT EXISTS '{user_name}'@'%' IDENTIFIED BY '{password}'",
        f"ALTER USER '{user_name}'@'%' IDENTIFIED BY '{password}'",
        f"GRANT ALL PRIVILEGES ON `{schema_name}`.* TO '{user_name}'@'%'",
        "FLUSH PRIVILEGES",
    ]
    report(
        "database-schema",
        f"Provisioning schema {schema_name} on the shared project database",
    )
    _database_engine.provision(
        DatabaseSchemaProvisionRequest(
            docker_client=docker_client,
            image=database.image,
            network_name=shared_network.name,
            host=server.name,
            credentials_volume=credentials_volume,
            statements=statements,
            error=PreviewOperationError,
        )
    )

    report("database", "Connecting the shared database to the preview network")
    _connect_shared_server(run_network, server)

    controller_store.record_shared_schema(
        sandbox_id=sandbox_id,
        project_id=project_key,
        owner_sandbox_id=sandbox_id,
        sharing=database.sharing.value,
        schema_name=schema_name,
        user_name=user_name,
        image=database.image,
        persistence=database.persistence.value,
    )
    credentials = {
        "username": user_name,
        "password": password,
        "root_password": "",
    }
    return credentials, schema_name


def _restart_shared_database(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    project_key: str,
    source_path: str,
    database: PreviewDependencyService,
    run_id: str,
) -> Container:
    """Brings the shared server back up and reattaches it to the run network.

    A restart can follow a daemon restart, so the endpoint on the preview
    network is reasserted rather than assumed.
    """
    server, _, _ = _shared_database_server(
        docker_client,
        settings,
        project_key=project_key,
        source_path=source_path,
        database=database,
        report=_ignore_progress,
    )
    run_network_name = f"orchestrator-preview-{run_id[:12]}"
    for network in _preview_networks(docker_client, run_id):
        if network.name == run_network_name:
            _connect_shared_server(network, server)
    server.reload()
    return server


def _connect_shared_server(run_network: Any, server: Container) -> None:
    """Aliases the shared server as `database` inside one preview network."""
    try:
        run_network.connect(server, aliases=["database"])
    except APIError as error:
        message = str(error).casefold()
        if "already exists" not in message and "already connected" not in message:
            raise


def _connect_sandbox_database_endpoint(
    docker_client: DockerClient,
    runtime: SandboxDatabaseRuntime,
    container: Container,
) -> None:
    """Join a runtime to the persistent internal network as a borrowed endpoint."""
    try:
        network = docker_client.networks.get(runtime.network_name)
        network.connect(container)
    except (NotFound, APIError) as error:
        message = str(error).casefold()
        if "already exists" not in message and "already connected" not in message:
            raise PreviewOperationError(
                409,
                f"Could not join sandbox database network '{runtime.network_name}'",
            ) from error


def _managed_preview_database(
    docker_client: DockerClient,
    controller_store: ControllerStore | None,
    sandbox_id: str,
) -> SandboxDatabaseRuntime | None:
    if controller_store is None:
        return None
    try:
        return sandbox_database_runtime(
            docker_client,
            controller_store,
            sandbox_id,
        )
    except SandboxDatabaseError as error:
        raise PreviewOperationError(error.status_code, error.detail) from error


def _release_shared_database(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
) -> dict[str, Any]:
    """Undoes one sandbox's claim on the shared server.

    An owner loses its schema only when the data is ephemeral and no guest is
    still attached to it. A guest loses only its own user. No new guest can be
    created, so the guest path here serves rows written before that rule and is
    the one place that still asks whether a row belongs to its sandbox.
    """
    record = controller_store.shared_schema(sandbox_id)
    if record is None:
        return {"released": False}

    project_key = str(record["project_id"])
    schema_name = str(record["schema_name"])
    user_name = str(record["user_name"])
    image = str(record["image"])
    owner = str(record["owner_sandbox_id"]) == sandbox_id
    ephemeral = str(record["persistence"]) == PreviewPersistence.EPHEMERAL.value
    siblings = [
        row
        for row in controller_store.shared_schemas_for_project(project_key)
        if str(row["sandbox_id"]) != sandbox_id
        and str(row["schema_name"]) == schema_name
    ]
    drop_schema = owner and ephemeral and not siblings

    names = _shared_database_names(project_key)
    server = _existing_shared_server(docker_client, names["container"])
    credentials_volume = _existing_volume(docker_client, names["credentials"])
    outcome: dict[str, Any] = {
        "released": True,
        "schema": schema_name,
        "dropped_schema": drop_schema,
        "kept_for_attached_sandboxes": len(siblings) if owner and ephemeral else 0,
    }
    applied = False
    if server is not None and server.status == "running" and credentials_volume is not None:
        statements = [f"DROP USER IF EXISTS '{user_name}'@'%'"]
        if drop_schema:
            statements.append(f"DROP DATABASE IF EXISTS `{schema_name}`")
        statements.append("FLUSH PRIVILEGES")
        try:
            _database_engine.drop(
                DatabaseDropRequest(
                    docker_client=docker_client,
                    image=image,
                    network_name=names["network"],
                    host=server.name,
                    credentials_volume=credentials_volume,
                    statements=statements,
                    error=PreviewOperationError,
                )
            )
            applied = True
        except (PreviewOperationError, DockerException) as error:
            # The sandbox is going away either way. Record the leftover so an
            # operator can see it instead of losing it silently.
            outcome["error"] = str(error)

    # The record tracks a schema that exists. It is dropped only when the schema
    # and user really went away; otherwise the leftover stays visible and the
    # next start of this sandbox reuses it instead of creating a duplicate.
    keep_record = not applied or (owner and not drop_schema)
    if not keep_record:
        controller_store.delete_shared_schema(sandbox_id)
    outcome["kept_record"] = keep_record
    outcome["pending_cleanup"] = not applied
    _stop_idle_shared_server(docker_client, controller_store, project_key)
    return outcome


def _shared_server_is_idle(
    controller_store: ControllerStore,
    project_key: str,
) -> bool:
    """True when no preview of this project is still running.

    Persistent schemas keep their records after their preview stops, so idleness
    is measured by active previews, not by records.
    """
    project_sandboxes = {
        str(sandbox["id"])
        for sandbox in controller_store.sandboxes()
        if str(sandbox["project_id"]) == project_key
    }
    return not any(
        str(run["sandbox_id"]) in project_sandboxes
        for run in controller_store.active_previews()
    )


def _stop_idle_shared_server(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_key: str,
) -> None:
    """Removes the shared server once no preview of the project is running.

    Only the container goes. Its volumes stay, so persistent schemas and the
    root credentials survive and the next start finds the same data.
    """
    if not _shared_server_is_idle(controller_store, project_key):
        return
    names = _shared_database_names(project_key)
    with shared_database_server_lock(names["container"]):
        server = _existing_shared_server(docker_client, names["container"])
        if server is not None:
            try:
                server.remove(force=True, v=True)
            except DockerException:
                return
        for network in docker_client.networks.list(names=[names["network"]]):
            if network.name != names["network"]:
                continue
            try:
                network.remove()
            except DockerException:
                continue


def _sharing_state(
    controller_store: ControllerStore,
    sandbox_id: str,
) -> DatabaseSharingState | None:
    record = controller_store.shared_schema(sandbox_id)
    if record is None:
        return None
    project_key = str(record["project_id"])
    owner_sandbox_id = str(record["owner_sandbox_id"])
    schema_name = str(record["schema_name"])
    names = {
        str(sandbox["id"]): str(sandbox["project_name"])
        for sandbox in controller_store.sandboxes()
    }
    attached = [
        names.get(str(row["sandbox_id"]), str(row["sandbox_id"])[:12])
        for row in controller_store.shared_schemas_for_project(project_key)
        if str(row["schema_name"]) == schema_name
        and str(row["sandbox_id"]) != owner_sandbox_id
    ]
    return DatabaseSharingState(
        sandbox_id=sandbox_id,
        sharing=PreviewSharing(str(record["sharing"])),
        schema_name=schema_name,
        owner_sandbox_id=owner_sandbox_id,
        owner_project_name=names.get(owner_sandbox_id, owner_sandbox_id[:12]),
        image=str(record["image"]),
        persistence=PreviewPersistence(str(record["persistence"])),
        server_container=_shared_database_names(project_key)["container"],
        attached_project_names=attached,
    )


def database_sharing_state(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> ProjectDatabaseSharing:
    """The database coupling of one sandbox."""
    project = _ready_project(docker_client, project_name, controller_store)
    return ProjectDatabaseSharing(
        project_name=project.name,
        sandbox_id=project.sandbox_id,
        current=_sharing_state(controller_store, project.sandbox_id),
    )


def _validate_sharing(config: PreviewConfiguration) -> None:
    database = config.services.get("database")
    if database is None or database.sharing is PreviewSharing.ISOLATED:
        return
    if config.mode is not PreviewMode.NATIVE:
        raise PreviewOperationError(
            422,
            "Shared databases are supported only for native previews",
        )
    if database.sharing is not PreviewSharing.SHARED_DATA:
        return
    # A guest wrote into the schema its owner also wrote. Only the copied local
    # folders tolerated that, because they shared one MySQL server per source
    # path. Every managed sandbox owns its own schema, so the mode has no
    # remaining meaning and no caller can opt into it.
    raise PreviewOperationError(422, _SHARED_DATA_UNAVAILABLE)
