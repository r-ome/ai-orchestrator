import re
import shlex
from typing import Any

from docker.client import DockerClient
from docker.errors import DockerException
from docker.models.containers import Container
from docker.types import Mount
from requests.exceptions import ReadTimeout

from app.containers.hardened import (
    Egress,
    HardenedContainerSpec,
    Rootfs,
    create_hardened,
)
from app.controller.store import ControllerStore
from app.dependency_cache import (
    _DEPENDENCY_READY_MARKER,
    _data_volume,
    _dependency_volume,
    _dependency_volume_ready,
    _lockfile_digest,
    _volume_runtime_files,
)
from app.labels import LABEL_SANDBOX_ID, LABEL_SERVICE
from app.previews._shared import _slug
from app.previews.config import PreviewSettings
from app.previews.detection import hashes
from app.previews.errors import PreviewOperationError
from app.previews.health import _wait_for_container_health, _wait_for_mysql_health
from app.previews.models import (
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewKind,
    PreviewNetworkAccess,
    PreviewPersistence,
    PreviewRuntime,
    PreviewSharing,
)
from app.previews.network import (
    PREVIEW_CONTAINER_PREFIX,
    _direct_ports,
    _gateway_proxy,
    _network,
    _preview_egress,
)
from app.previews.progress import ProgressReporter, _ignore_progress, _timed_step
from app.previews.protected_files import (
    _MASKED_ENVIRONMENT_NAMES,
    _environment_masks,
    _exclude_preview_masks,
)
from app.previews.resources import _ensure_preview_image
from app.previews.runtimes.environment import _secret_environment
from app.previews.sharing import (
    _attach_shared_database,
    _connect_sandbox_database_endpoint,
    _database_engine,
)
from app.sandboxes.database import (
    DatabaseConnectionRequest,
    DatabaseMigrationRequest,
    DatabaseProvisionRequest,
    SandboxDatabaseError,
    SandboxDatabaseRuntime,
    sandbox_database_runtime,
)
from app.sandboxes.git import run_git


# Controller metadata a preview has no business reading. A directory, so tmpfs
# masks it.
_MASKED_DIRECTORIES = (".orchestrator",)
# Build output a live preview must write somewhere other than the sandbox
# worktree, or every completion report fails the dirty-tree check on artifacts
# the project happens not to gitignore.
_BUILD_OUTPUT_PATHS = ("dist", ".astro", ".next")
_NODE_RUNTIMES = {"astro", "vite", "nextjs"}
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")


def _start_native(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
    host_port: int,
    expected_protected_hashes: dict[str, str] | None = None,
    progress: ProgressReporter | None = None,
    controller_store: ControllerStore | None = None,
    project_key: str = "",
    source_path: str = "",
    secrets: dict[str, str] | None = None,
    kind: PreviewKind = PreviewKind.LIVE,
    commit_sha: str = "",
) -> dict[str, Any]:
    report = progress or _ignore_progress
    report("image", f"Checking runtime image {config.image}")
    _ensure_preview_image(docker_client, config.image)
    database = config.services.get("database")
    data_volumes: list[Any] = []
    if kind is PreviewKind.TASK:
        with _timed_step(
            report, "workspace", f"Exporting sandbox commit {commit_sha[:12]}"
        ) as finish:
            _ensure_preview_image(docker_client, settings.git_image)
            workspace = _data_volume(
                docker_client,
                run_id,
                "runtime-workspace",
                labels,
                False,
            )
            data_volumes.append(workspace)
            _export_commit(
                docker_client,
                settings.git_image,
                project_volume,
                workspace.name,
                commit_sha,
            )
            workspace_volume = workspace.name
            finish("Runtime workspace holds the task commit")
    else:
        # The sandbox volume itself, so an agent's edit reaches the development
        # server without a copy and without a restart.
        report("workspace", "Mounting the sandbox for a live preview")
        _exclude_preview_masks(
            docker_client,
            settings.inspection_image,
            project_volume,
        )
        workspace_volume = project_volume
        report("workspace", "Sandbox workspace is ready")
    mounts = _environment_masks(docker_client, settings, workspace_volume)
    tmpfs = {
        "/tmp": "rw,nosuid,size=256m",
        **{
            f"/workspace/{path}": "rw,nosuid,size=1m"
            for path in _MASKED_DIRECTORIES
        },
    }
    volumes: dict[str, dict[str, str]] = {
        workspace_volume: {"bind": "/workspace", "mode": "rw"},
    }
    if kind is PreviewKind.LIVE and config.runtime.value in _NODE_RUNTIMES:
        # Build output belongs to the run, not to the sandbox worktree, or a
        # completion report fails on artifacts the project does not gitignore.
        for path in _BUILD_OUTPUT_PATHS:
            output = _data_volume(
                docker_client,
                run_id,
                f"build-{_slug(path)}",
                labels,
                False,
            )
            volumes[output.name] = {"bind": f"/workspace/{path}", "mode": "rw"}
            data_volumes.append(output)
    dependency_reused = False
    if config.runtime.value in _NODE_RUNTIMES:
        lockfile_digest = _lockfile_digest(
            _volume_runtime_files(docker_client, workspace_volume, settings)
        )
        dependency = _dependency_volume(
            docker_client,
            labels[LABEL_SANDBOX_ID],
            lockfile_digest,
            labels,
        )
        dependency_reused = _dependency_volume_ready(
            docker_client,
            settings,
            dependency.name,
        )
        volumes[dependency.name] = {"bind": "/workspace/node_modules", "mode": "rw"}
        data_volumes.append(dependency)
    elif config.runtime.value == "fastapi":
        dependency = _data_volume(docker_client, run_id, "python-venv", labels, False)
        volumes[dependency.name] = {"bind": "/opt/venv", "mode": "rw"}
        data_volumes.append(dependency)

    application_environment = _native_runtime_environment(config)
    application_environment.update(_secret_environment(config, secrets or {}))
    managed_database: SandboxDatabaseRuntime | None = None
    if controller_store is not None:
        try:
            managed_database = sandbox_database_runtime(
                docker_client,
                controller_store,
                labels[LABEL_SANDBOX_ID],
            )
        except SandboxDatabaseError as error:
            raise PreviewOperationError(error.status_code, error.detail) from error
    if managed_database is not None:
        application_environment.update(managed_database.environment)
        volumes.update(managed_database.volumes)

    if config.install_command:
        if config.runtime.value in _NODE_RUNTIMES:
            npm_cache = _data_volume(
                docker_client,
                run_id,
                "npm-cache",
                {**labels, LABEL_SERVICE: "npm-cache"},
                True,
            )
            volumes[npm_cache.name] = {"bind": "/root/.npm", "mode": "rw"}
            data_volumes.append(npm_cache)
        if dependency_reused:
            report(
                "dependencies",
                "Dependency volume already installed for this lockfile; skipping install",
                duration_ms=0,
            )
        else:
            with _timed_step(
                report,
                "dependencies",
                "Running the approved dependency installation command",
            ) as finish:
                install = config.install_command
                if config.runtime.value == "fastapi":
                    install = (
                        f"python -m venv /opt/venv\n. /opt/venv/bin/activate\n{install}"
                    )
                _run_prepare(
                    docker_client,
                    settings,
                    image=config.image,
                    command=f"set -eu\n{install}",
                    volumes=volumes,
                    mounts=mounts,
                    labels=labels,
                    environment=application_environment,
                    size_path=(
                        "/workspace/node_modules"
                        if config.runtime.value in _NODE_RUNTIMES
                        else "/opt/venv" if config.runtime.value == "fastapi" else None
                    ),
                    completion_marker=(
                        f"/workspace/node_modules/{_DEPENDENCY_READY_MARKER}"
                        if config.runtime.value in _NODE_RUNTIMES
                        else None
                    ),
                )
                finish("Dependency installation completed")
            if expected_protected_hashes is not None:
                report(
                    "protected-files",
                    "Checking protected runtime files after installation",
                )
                prepared_files = _volume_runtime_files(
                    docker_client,
                    project_volume,
                    settings,
                )
                if hashes(prepared_files) != expected_protected_hashes:
                    raise PreviewOperationError(
                        409,
                        "Dependency installation changed protected runtime files; "
                        "inspect again",
                    )

    if config.runtime.value in _NODE_RUNTIMES:
        # Vite's dependency optimizer creates node_modules/.vite on first run,
        # so a read-only mount fails with ENOENT and leaves the preview serving
        # unoptimized dependencies. The coding agent still mounts this volume
        # read-only; the install authority boundary that matters is the agent's.
        volumes[dependency.name] = {"bind": "/workspace/node_modules", "mode": "rw"}

    report("network", f"Creating {config.network_access.value} preview network")
    network = _network(docker_client, run_id, labels, config.network_access)
    containers: list[Container] = []
    if managed_database is not None:
        report(
            "database",
            f"Using sandbox database {managed_database.db_name}",
        )
    elif database is not None and database.sharing is not PreviewSharing.ISOLATED:
        if controller_store is None or not project_key:
            raise PreviewOperationError(
                422,
                "A shared database needs the controller store and project identity",
            )
        credentials, schema_name = _attach_shared_database(
            docker_client,
            controller_store,
            settings,
            sandbox_id=labels[LABEL_SANDBOX_ID],
            project_key=project_key,
            source_path=source_path,
            database=database,
            run_network=network,
            report=report,
        )
        application_environment.update(
            _native_service_environment(
                config,
                database,
                credentials,
                database_name=schema_name,
            )
        )
        if config.initialize.commands:
            report("initialize", "Running approved migration and seed commands")
            containers.append(
                _run_initialization(
                    docker_client,
                    settings,
                    image=config.image,
                    commands=config.initialize.commands,
                    runtime=config.runtime.value,
                    environment=application_environment,
                    volumes=volumes,
                    mounts=mounts,
                    tmpfs=tmpfs,
                    labels=labels,
                    network=network,
                    run_id=run_id,
                )
            )
            report("initialize", "Database initialization completed")
    elif database is not None:
        report("database-image", f"Checking database image {database.image}")
        _ensure_preview_image(docker_client, database.image)
        persistent = database.persistence is PreviewPersistence.PERSISTENT
        database_labels = {**labels, LABEL_SERVICE: "database"}
        database_volume = _data_volume(
            docker_client,
            run_id,
            "database",
            database_labels,
            persistent,
        )
        data_volumes.append(database_volume)
        credentials_volume = _data_volume(
            docker_client,
            run_id,
            "database-credentials",
            {**labels, LABEL_SERVICE: "database-credentials"},
            persistent,
        )
        data_volumes.append(credentials_volume)
        report("database", "Creating MySQL database container")
        provision = _database_engine.provision(
            DatabaseProvisionRequest(
                docker_client=docker_client,
                image=database.image,
                database=database.database,
                container_name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-database",
                labels=database_labels,
                data_volume=database_volume.name,
                credentials_volume=credentials_volume,
                network_name=network.name,
                memory_limit=settings.preview_memory,
                nano_cpus=1_000_000_000,
                pids_limit=256,
                error=PreviewOperationError,
            )
        )
        if provision is None:
            raise RuntimeError("MySQL container provisioning returned no container")
        credentials = provision.credentials
        application_environment.update(
            _native_service_environment(
                config,
                database,
                credentials,
            )
        )
        database_container = provision.container
        network.disconnect(database_container)
        network.connect(database_container, aliases=["database"])
        database_container.start()
        containers.append(database_container)
        report("database-health", "Waiting for MySQL health check")
        _wait_for_mysql_health(
            database_container,
            timeout_seconds=settings.prepare_timeout_seconds,
        )
        report("database-health", "MySQL is healthy")

        if config.initialize.commands:
            report("initialize", "Running approved migration and seed commands")
            initializer = _run_initialization(
                docker_client,
                settings,
                image=config.image,
                commands=config.initialize.commands,
                runtime=config.runtime.value,
                environment=application_environment,
                volumes=volumes,
                mounts=mounts,
                tmpfs=tmpfs,
                labels=labels,
                network=network,
                run_id=run_id,
            )
            containers.append(initializer)
            report("initialize", "Database initialization completed")

    start = config.start_command
    if config.runtime.value == "fastapi":
        start = f". /opt/venv/bin/activate\nexec {start}"
    else:
        start = f"exec {start}"
    with _timed_step(report, "container", "Creating application container") as finish:
        container = create_hardened(docker_client, HardenedContainerSpec(
            image=config.image,
            command=["sh", "-lc", f"set -eu\n{start}"],
            name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-app",
            working_dir="/workspace",
            environment=application_environment,
            labels={**labels, LABEL_SERVICE: "app"},
            volumes=volumes,
            mounts=mounts or None,
            tmpfs_size="256m",
            extra_tmpfs={key: value for key, value in tmpfs.items() if key != "/tmp"},
            network=network.name,
            egress=_preview_egress(config.network_access),
            ports=_direct_ports(config, host_port),
            restart_policy={"Name": "no"},
            mem_limit=settings.preview_memory,
            nano_cpus=1_000_000_000,
            pids_limit=256,
        ))
        network.disconnect(container)
        network.connect(container, aliases=["app"])
        if managed_database is not None and managed_database.engine != "sqlite":
            _connect_sandbox_database_endpoint(
                docker_client,
                managed_database,
                container,
            )
        container.start()
        _wait_for_container_health(
            container,
            timeout_seconds=settings.prepare_timeout_seconds,
        )
        finish("Application container started")
    containers.append(container)
    networks = [network]
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        report("gateway", "Creating the loopback preview gateway")
        gateway, gateway_network, gateway_volume = _gateway_proxy(
            docker_client,
            settings.inspection_image,
            network,
            "app",
            config.container_port,
            host_port,
            labels,
            run_id,
        )
        containers.append(gateway)
        networks.append(gateway_network)
        data_volumes.append(gateway_volume)
        report("gateway", "Loopback preview gateway started")
    return {
        "containers": containers,
        "networks": networks,
        "volumes": data_volumes,
        "images": [],
        "borrowed_networks": (
            [docker_client.networks.get(managed_database.network_name)]
            if managed_database is not None and managed_database.engine != "sqlite"
            else []
        ),
    }


def _export_commit(
    docker_client: DockerClient,
    git_image: str,
    project_volume: str,
    workspace_volume: str,
    commit_sha: str,
) -> None:
    """Replaces the run workspace with the tree of one sandbox commit.

    `git archive` is reproducible, leaves `.git` behind, and skips everything
    the repository ignores. The workspace is emptied first so a re-export after
    a restart cannot leave a file the commit no longer contains. Env files are
    deleted at every depth: a commit may carry one, and no preview reads them.
    """
    if not _COMMIT_PATTERN.match(commit_sha):
        raise PreviewOperationError(422, "Task commit is not a commit hash")
    name_clauses = " -o ".join(
        f"-name {shlex.quote(name)}" for name in _MASKED_ENVIRONMENT_NAMES
    )
    script = (
        "set -eu\n"
        "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf {} +\n"
        f"git -C /source archive --format=tar {commit_sha} | tar -C /workspace -xf -\n"
        f"find /workspace -type f \\( {name_clauses} \\) -delete\n"
    )
    run_git(
        docker_client,
        image=git_image,
        script=script,
        volumes={
            project_volume: {"bind": "/source", "mode": "ro"},
            workspace_volume: {"bind": "/workspace", "mode": "rw"},
        },
    )


def _native_service_environment(
    config: PreviewConfiguration,
    database: PreviewDependencyService,
    credentials: dict[str, str],
    *,
    database_name: str = "",
) -> dict[str, str]:
    return _database_engine.connection_url(
        DatabaseConnectionRequest(
            config=config,
            database=database,
            credentials=credentials,
            database_name=database_name,
            error=PreviewOperationError,
        )
    )


def _native_runtime_environment(config: PreviewConfiguration) -> dict[str, str]:
    if config.runtime is PreviewRuntime.ASTRO:
        return {"ASTRO_TELEMETRY_DISABLED": "1"}
    return {}


def _run_initialization(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    image: str,
    commands: list[str],
    runtime: str,
    environment: dict[str, str],
    volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    network: Any,
    run_id: str,
    mounts: list[Mount] | None = None,
    tmpfs: dict[str, str] | None = None,
) -> Container:
    return _database_engine.run_migrations(
        DatabaseMigrationRequest(
            docker_client=docker_client,
            settings=settings,
            image=image,
            commands=commands,
            runtime=runtime,
            environment=environment,
            volumes=volumes,
            labels=labels,
            network=network,
            run_id=run_id,
            error=PreviewOperationError,
            mounts=mounts,
            tmpfs=tmpfs,
        )
    )


def _run_prepare(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    image: str,
    command: str,
    volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    size_path: str | None,
    completion_marker: str | None = None,
    environment: dict[str, str] | None = None,
    mounts: list[Mount] | None = None,
) -> None:
    container: Container | None = None
    try:
        checked_command = command
        if size_path:
            maximum_kib = settings.maximum_dependency_bytes // 1024
            checked_command += (
                f"\nused_kib=$(du -sk {shlex.quote(size_path)} | awk '{{print $1}}')"
                f"\nif [ \"$used_kib\" -gt {maximum_kib} ]; then"
                " echo 'Installed dependencies exceed the configured size limit' >&2;"
                " exit 73; fi"
            )
        if completion_marker:
            checked_command += f"\ntouch {shlex.quote(completion_marker)}"
        container = create_hardened(docker_client, HardenedContainerSpec(
            image=image,
            command=["sh", "-lc", checked_command],
            working_dir="/workspace",
            network="bridge",
            egress=Egress.PROVIDER,
            environment=environment,
            labels={**labels, LABEL_SERVICE: "prepare"},
            volumes=volumes,
            mounts=mounts or None,
            rootfs=Rootfs.WRITABLE,
            # No /tmp tmpfs. A package manager unpacks into /tmp, and this
            # container had a disk-backed /tmp before the boundary owned it.
            tmpfs_size=None,
            mem_limit=settings.preview_memory,
            pids_limit=256,
        ))
        container.start()
        try:
            result = container.wait(timeout=settings.prepare_timeout_seconds)
        except ReadTimeout as error:
            container.stop(timeout=2)
            raise PreviewOperationError(
                408,
                f"Dependency installation exceeded {settings.prepare_timeout_seconds} seconds",
            ) from error
        exit_code = int(result.get("StatusCode", 1))
        if exit_code != 0:
            logs = container.logs(stdout=True, stderr=True, tail=100)
            if isinstance(logs, bytes):
                detail = logs.decode("utf-8", errors="replace")[-8_192:]
            else:
                detail = str(logs)[-8_192:]
            raise PreviewOperationError(
                422,
                f"Dependency installation failed with code {exit_code}: {detail}",
            )
    finally:
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except DockerException:
                pass
