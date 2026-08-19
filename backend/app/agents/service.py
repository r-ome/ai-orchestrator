import hashlib
import re
import shlex
from typing import Any
from uuid import uuid4

from docker.client import DockerClient
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.volumes import Volume

from app.agents.config import AgentSettings
from app.containers.hardened import (
    Egress,
    HardenedContainerSpec,
    create_hardened,
)
from app.controller.store import (
    ActiveAgentRunExists,
    ControllerStore,
    SandboxWriterAdmissionError,
)
from app.errors import OperationError
from app.labels import LABEL_CONTROLLER_MANAGED, LABEL_KIND, LABEL_RUN_ID, LABEL_SANDBOX_ID
from app.agents.models import (
    AgentProvider,
    CleanupAgentsResponse,
    CodingAgent,
    CodingAgentsResponse,
    CreateAgentRequest,
    ReplaceAgentRequest,
    StopAgentResponse,
)
from app.previews.config import get_preview_settings
from app.previews.errors import PreviewOperationError
from app.previews.service import (
    _dependency_volume,
    _lockfile_digest,
    _volume_runtime_files,
)
from app.projects.service import (
    ProjectOperationError,
    ensure_sandbox_registered,
    ensure_git_baseline,
    inspect_registered_project,
)
from app.sandboxes.database import SandboxDatabaseError, sandbox_database_runtime

LABEL_MANAGED = "orchestrator.agent.managed"
LABEL_PROVIDER = "orchestrator.agent.provider"
LABEL_PROJECT_NAME = "orchestrator.agent.project-name"
LABEL_PROJECT_VOLUME = "orchestrator.agent.project-volume"
LABEL_CREDENTIAL_PROFILE = "orchestrator.agent.credential-profile"
LABEL_CREDENTIAL_VOLUME = "orchestrator.agent.credential-volume"
LABEL_COMMAND = "orchestrator.agent.command"
LABEL_CREDENTIAL_MANAGED = "orchestrator.agent.credential.managed"
AGENT_CONTAINER_PREFIX = "orchestrator-agent-"
WORKSPACE_DIRECTORY = "/workspace"
CREDENTIAL_DIRECTORY = "/auth"
TMUX_SESSION_NAME = "agent"
IDLE_COMMAND = [
    "sh",
    "-c",
    (
        "umask 077; mkdir -p \"$HOME\"; trap 'exit 0' TERM INT; "
        "while :; do sleep 3600 & wait $!; done"
    ),
]


class AgentOperationError(OperationError):
    """An agent operation failed."""


def create_agent(
    docker_client: DockerClient,
    settings: AgentSettings,
    request: CreateAgentRequest,
    controller_store: ControllerStore,
) -> CodingAgent:
    try:
        project = inspect_registered_project(
            docker_client,
            request.project_name,
            controller_store,
        )
        if not project.ready:
            raise ProjectOperationError(
                409,
                f"Project '{request.project_name}' is not ready",
            )
        sandbox_id, _, project = ensure_sandbox_registered(
            docker_client,
            controller_store,
            request.project_name,
            project=project,
        )
    except ProjectOperationError as error:
        raise AgentOperationError(error.status_code, error.detail) from error
    # Reconcile a stale row or reject a live environment before claiming the
    # one active-agent slot for this attempt.
    _reject_active_agent(docker_client, controller_store, project.volume_name, sandbox_id)

    provider = settings.provider(request.provider)
    agent_token = uuid4().hex[:12]
    run_id = uuid4().hex
    name = f"{AGENT_CONTAINER_PREFIX}{request.provider.value}-{agent_token}"
    credential_volume_name = _credential_volume_name(
        request.provider,
        request.credential_profile,
    )
    labels = {
        LABEL_MANAGED: "true",
        LABEL_PROVIDER: request.provider.value,
        LABEL_PROJECT_NAME: project.name,
        LABEL_PROJECT_VOLUME: project.volume_name,
        LABEL_CREDENTIAL_PROFILE: request.credential_profile,
        LABEL_CREDENTIAL_VOLUME: credential_volume_name,
        LABEL_COMMAND: " ".join(provider.command),
        LABEL_SANDBOX_ID: sandbox_id,
        LABEL_RUN_ID: run_id,
        LABEL_KIND: "agent",
        LABEL_CONTROLLER_MANAGED: "true",
    }

    try:
        controller_store.start_agent_run(
            run_id=run_id,
            sandbox_id=sandbox_id,
            provider=request.provider.value,
        )
    except SandboxWriterAdmissionError as error:
        raise AgentOperationError(409, str(error)) from error
    except ActiveAgentRunExists as error:
        raise AgentOperationError(
            409,
            f"Sandbox '{project.name}' already has an active coding agent",
        ) from error

    container: Container | None = None
    try:
        # The durable row precedes every sandbox mutation. Git baseline setup
        # can create a repository and commit, while dependency resolution can
        # create a sandbox-owned volume.
        _ensure_sandbox_git_baseline(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
            project_volume=project.volume_name,
        )
        credential_volume = _credential_volume(
            docker_client,
            request.provider,
            request.credential_profile,
        )
        dependency_volume = _agent_dependency_volume(
            docker_client,
            sandbox_id=sandbox_id,
            project_volume=project.volume_name,
            labels=labels,
        )
        try:
            database_runtime = sandbox_database_runtime(
                docker_client,
                controller_store,
                sandbox_id,
            )
        except SandboxDatabaseError as error:
            raise AgentOperationError(error.status_code, error.detail) from error
        environment = {
            provider.credential_environment_variable: CREDENTIAL_DIRECTORY,
            "HOME": "/tmp/home",
            "TERM": "xterm-256color",
        }
        volumes = {
            project.volume_name: {
                "bind": WORKSPACE_DIRECTORY,
                "mode": "rw",
            },
            credential_volume.name: {
                "bind": CREDENTIAL_DIRECTORY,
                "mode": "rw",
            },
            dependency_volume.name: {
                "bind": f"{WORKSPACE_DIRECTORY}/node_modules",
                "mode": "ro",
            },
        }
        if database_runtime is not None:
            environment.update(database_runtime.environment)
            volumes.update(database_runtime.volumes)
        container = create_hardened(
            docker_client,
            HardenedContainerSpec(
                image=provider.image,
                command=IDLE_COMMAND,
                name=name,
                # The agent in this container calls a model API, so it keeps the
                # default bridge. It is also connected to its database network
                # below, which `network_mode="none"` would refuse.
                egress=Egress.PROVIDER,
                auto_remove=True,
                pids_limit=512,
                mem_limit=settings.agent_memory,
                working_dir=WORKSPACE_DIRECTORY,
                environment=environment,
                labels=labels,
                volumes=volumes,
                tmpfs_size="512m",
            ),
        )
        if database_runtime is not None and database_runtime.engine != "sqlite":
            docker_client.networks.get(database_runtime.network_name).connect(container)
        container.start()
        controller_store.update_agent_run(
            run_id,
            status="running",
            container_id=container.id,
        )
    except ImageNotFound as error:
        controller_store.update_agent_run(run_id, status="failed")
        _remove_created_container(container)
        raise AgentOperationError(
            424,
            f"Agent image '{provider.image}' is not available",
        ) from error
    except PreviewOperationError as error:
        controller_store.update_agent_run(run_id, status="failed")
        _remove_created_container(container)
        raise AgentOperationError(error.status_code, error.detail) from error
    except DockerException:
        controller_store.update_agent_run(run_id, status="failed")
        _remove_created_container(container)
        raise
    except Exception:
        controller_store.update_agent_run(run_id, status="failed")
        _remove_created_container(container)
        raise

    return _agent_from_container(container, settings)


def list_agents(
    docker_client: DockerClient,
    settings: AgentSettings,
) -> CodingAgentsResponse:
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"{LABEL_MANAGED}=true"},
    )
    agents = [_agent_from_container(container, settings) for container in containers]
    agents.sort(key=lambda agent: agent.created_at, reverse=True)
    return CodingAgentsResponse(count=len(agents), agents=agents)


def inspect_agent(
    docker_client: DockerClient,
    settings: AgentSettings,
    agent_id: str,
) -> CodingAgent:
    container = get_managed_agent_container(docker_client, agent_id)
    return _agent_from_container(container, settings)


def stop_agent(
    docker_client: DockerClient,
    agent_id: str,
    *,
    timeout_seconds: int = 2,
    controller_store: ControllerStore | None = None,
) -> StopAgentResponse:
    container = get_managed_agent_container(docker_client, agent_id)
    agent_short_id = container.short_id
    agent_name = container.name
    if container.status == "running":
        container.stop(timeout=timeout_seconds)
    else:
        container.remove(force=True)
    if controller_store is not None:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        run_id = labels.get(LABEL_RUN_ID, "")
        if run_id:
            controller_store.update_agent_run(run_id, status="stopped")
    return StopAgentResponse(id=agent_short_id, name=agent_name, stopped=True)


def replace_agent(
    docker_client: DockerClient,
    settings: AgentSettings,
    agent_id: str,
    request: ReplaceAgentRequest,
    controller_store: ControllerStore,
) -> CodingAgent:
    current = get_managed_agent_container(docker_client, agent_id)
    labels = (current.attrs.get("Config") or {}).get("Labels") or {}
    project_name = labels.get(LABEL_PROJECT_NAME, "")
    if not project_name:
        raise AgentOperationError(500, "Agent project label is missing")
    stop_agent(
        docker_client,
        agent_id,
        timeout_seconds=request.timeout_seconds,
        controller_store=controller_store,
    )
    return create_agent(
        docker_client,
        settings,
        CreateAgentRequest(
            project_name=project_name,
            provider=request.provider,
            credential_profile=request.credential_profile,
        ),
        controller_store,
    )


def cleanup_agents(docker_client: DockerClient) -> CleanupAgentsResponse:
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"{LABEL_MANAGED}=true"},
    )
    removed_count = 0
    for container in containers:
        try:
            if container.status == "running":
                container.stop(timeout=2)
            else:
                container.remove(force=True)
            removed_count += 1
        except NotFound:
            # An auto-remove container can disappear between list and cleanup.
            removed_count += 1
        except DockerException:
            # Continue so one failed removal does not block later containers.
            continue
    return CleanupAgentsResponse(removed_count=removed_count)


def get_managed_agent_container(
    docker_client: DockerClient,
    agent_id: str,
) -> Container:
    try:
        container = docker_client.containers.get(agent_id)
    except NotFound as error:
        raise AgentOperationError(404, f"Agent '{agent_id}' was not found") from error
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    if labels.get(LABEL_MANAGED) != "true":
        raise AgentOperationError(404, f"Agent '{agent_id}' was not found")
    return container


def start_agent_exec(
    docker_client: DockerClient,
    container: Container,
    settings: AgentSettings,
) -> tuple[str, Any]:
    if container.status != "running":
        raise AgentOperationError(409, "Agent container is not running")
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    try:
        provider = AgentProvider(labels.get(LABEL_PROVIDER, ""))
    except ValueError as error:
        raise AgentOperationError(500, "Agent provider label is invalid") from error
    provider_command = settings.provider(provider).command
    shell_command = shlex.join(("exec", *provider_command))
    tmux_command = (
        "set -eu\n"
        f"if ! tmux has-session -t {TMUX_SESSION_NAME} 2>/dev/null; then\n"
        f"  tmux new-session -d -s {TMUX_SESSION_NAME} "
        f"{shlex.quote(shell_command)}\n"
        "fi\n"
        f"exec tmux attach-session -t {TMUX_SESSION_NAME}\n"
    )
    result = docker_client.api.exec_create(
        container.id,
        ["sh", "-lc", tmux_command],
        stdin=True,
        tty=True,
        privileged=False,
        workdir=WORKSPACE_DIRECTORY,
    )
    exec_id = result["Id"]
    stream = docker_client.api.exec_start(
        exec_id,
        detach=False,
        tty=True,
        socket=True,
    )
    return exec_id, stream


def detach_agent_terminal(container: Container) -> None:
    """Detach terminal clients while leaving the agent and tmux server alive."""
    container.exec_run(
        ["tmux", "detach-client", "-s", TMUX_SESSION_NAME],
        stdout=False,
        stderr=False,
    )


def resize_agent_exec(
    docker_client: DockerClient,
    exec_id: str,
    *,
    columns: int,
    rows: int,
) -> None:
    docker_client.api.exec_resize(exec_id, height=rows, width=columns)


def inspect_agent_exec(docker_client: DockerClient, exec_id: str) -> int | None:
    result = docker_client.api.exec_inspect(exec_id)
    exit_code = result.get("ExitCode")
    return int(exit_code) if exit_code is not None else None


def _agent_dependency_volume(
    docker_client: DockerClient,
    *,
    sandbox_id: str,
    project_volume: str,
    labels: dict[str, str],
) -> Volume:
    """Resolves the sandbox's dependency volume for a read-only agent mount.

    Names the volume from the sandbox and its current lockfile digest, the
    same way a preview install would, so the coding agent can read whatever
    the controller has already installed without gaining install authority
    itself (ADR 0003). A preview may not have run yet, in which case the
    volume does not exist; get-or-create makes it empty rather than failing
    agent creation.
    """
    preview_settings = get_preview_settings()
    lockfile_digest = _lockfile_digest(
        _volume_runtime_files(docker_client, project_volume, preview_settings)
    )
    return _dependency_volume(docker_client, sandbox_id, lockfile_digest, labels)


def _ensure_sandbox_git_baseline(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    project_volume: str,
) -> None:
    """Ensures the sandbox volume has a git baseline commit before an agent starts.

    Runs on demand, at agent creation, rather than at project registration:
    every sandbox needs a baseline for its coding agent to commit against
    (ADR 0002), including sandboxes registered before this existed. Skips
    the container spawn once a baseline is recorded, so a second agent
    creation on the same sandbox costs one read. GIT_BASELINE_SCRIPT is
    idempotent, so a failure here is safe to retry on the next agent
    creation; it is not swallowed, since a coding agent on a sandbox with
    no git repository cannot produce a task branch.
    """
    if controller_store.sandbox_baseline_commit(sandbox_id):
        return
    git_image = get_preview_settings().git_image
    baseline_commit = ensure_git_baseline(docker_client, git_image, project_volume)
    controller_store.set_sandbox_baseline_commit(
        sandbox_id=sandbox_id,
        baseline_commit=baseline_commit,
    )


def _credential_volume(
    docker_client: DockerClient,
    provider: AgentProvider,
    profile: str,
) -> Volume:
    volume_name = _credential_volume_name(provider, profile)
    try:
        volume = docker_client.volumes.get(volume_name)
    except NotFound:
        try:
            return docker_client.volumes.create(
                name=volume_name,
                driver="local",
                labels={
                    LABEL_CREDENTIAL_MANAGED: "true",
                    LABEL_PROVIDER: provider.value,
                    LABEL_CREDENTIAL_PROFILE: profile,
                },
            )
        except APIError as error:
            response_status = getattr(
                getattr(error, "response", None),
                "status_code",
                0,
            )
            if response_status != 409:
                raise
            return docker_client.volumes.get(volume_name)

    labels = volume.attrs.get("Labels") or {}
    if (
        labels.get(LABEL_CREDENTIAL_MANAGED) != "true"
        or labels.get(LABEL_PROVIDER) != provider.value
        or labels.get(LABEL_CREDENTIAL_PROFILE) != profile
    ):
        raise AgentOperationError(
            409,
            f"Docker volume '{volume_name}' is not the requested credential profile",
        )
    return volume


credential_volume = _credential_volume


def _credential_volume_name(provider: AgentProvider, profile: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", profile.casefold()).strip("-._")
    slug = slug[:32] or "default"
    digest = hashlib.sha256(f"{provider.value}:{profile}".encode()).hexdigest()[:8]
    return f"orchestrator-agent-auth-{provider.value}-{slug}-{digest}"


def _agent_from_container(
    container: Container,
    settings: AgentSettings,
) -> CodingAgent:
    attrs = container.attrs
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    try:
        provider = AgentProvider(labels.get(LABEL_PROVIDER, ""))
    except ValueError as error:
        raise AgentOperationError(500, "Agent provider label is invalid") from error
    command = list(settings.provider(provider).command)
    return CodingAgent(
        id=container.id,
        run_id=labels.get(LABEL_RUN_ID, container.id),
        sandbox_id=labels.get(LABEL_SANDBOX_ID, ""),
        short_id=container.short_id,
        name=container.name,
        provider=provider,
        image=config.get("Image", ""),
        command=command,
        status=container.status,
        created_at=attrs.get("Created", ""),
        project_name=labels.get(LABEL_PROJECT_NAME, ""),
        project_volume=labels.get(LABEL_PROJECT_VOLUME, ""),
        credential_profile=labels.get(LABEL_CREDENTIAL_PROFILE, ""),
        credential_volume=labels.get(LABEL_CREDENTIAL_VOLUME, ""),
        workspace=WORKSPACE_DIRECTORY,
        websocket_url=f"/agents/{container.id}/ws",
    )


def _remove_created_container(container: Container | None) -> None:
    if container is None:
        return
    try:
        container.remove(force=True)
    except DockerException:
        pass


def _reject_active_agent(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_volume: str,
    sandbox_id: str,
) -> None:
    containers = docker_client.containers.list(
        all=True,
        filters={"label": f"{LABEL_MANAGED}=true"},
    )
    for container in containers:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(LABEL_PROJECT_VOLUME) != project_volume:
            continue
        if container.status in {"created", "running", "restarting", "paused"}:
            raise AgentOperationError(
                409,
                "Sandbox already has an active coding agent; use replace explicitly",
            )

    recorded = controller_store.active_agent(sandbox_id)
    if recorded is None:
        return
    container_id = recorded.get("container_id")
    try:
        container = docker_client.containers.get(container_id) if container_id else None
    except NotFound:
        container = None
    if container is None or container.status not in {
        "created",
        "running",
        "restarting",
        "paused",
    }:
        controller_store.update_agent_run(str(recorded["id"]), status="missing")
        return
    raise AgentOperationError(
        409,
        "Sandbox already has an active coding agent; use replace explicitly",
    )
