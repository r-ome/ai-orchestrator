import hashlib
from uuid import NAMESPACE_URL, uuid5

from docker.client import DockerClient
from docker.errors import NotFound

from app.controller.store import ControllerStore
from app.projects.models import ProjectRegistration
from app.sandboxes.models import SandboxLifecycleStatus
from app.sandboxes.git import run_git
from app.sandboxes.naming import validate_ownership


# Directories the controller and its agents create inside a sandbox. They are
# not the project's files and must never reach a task branch or a review diff.
SANDBOX_SCAFFOLDING = (".agent", ".claude", ".orchestrator")

GIT_BASELINE_SCRIPT = (
    "set -eu\n"
    "cd /project\n"
    "if [ -f .git ]; then\n"
    "  echo 'Cannot create a sandbox baseline from a linked worktree or submodule: .git is a gitdir pointer.' >&2\n"
    "  exit 42\n"
    "fi\n"
    "if [ ! -d .git ]; then\n"
    "  git init -q -b main\n"
    "fi\n"
    'git config user.name "orchestrator"\n'
    'git config user.email "orchestrator@localhost"\n'
    "mkdir -p .git/info\n"
    "touch .git/info/exclude\n"
    f'for scaffold in {" ".join(SANDBOX_SCAFFOLDING)}; do\n'
    '  if ! grep -qxF "/$scaffold/" .git/info/exclude; then\n'
    '    printf "/%s/\\n" "$scaffold" >> .git/info/exclude\n'
    "  fi\n"
    "done\n"
    "if ! git rev-parse HEAD >/dev/null 2>&1; then\n"
    "  git add -A\n"
    '  git commit -q -m "sandbox baseline" --allow-empty\n'
    "fi\n"
    "git rev-parse HEAD\n"
)


class ProjectOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def inspect_registered_project(
    docker_client: DockerClient,
    project_name: str,
    controller_store: ControllerStore,
) -> ProjectRegistration:
    sandbox = controller_store.sandbox(project_name)
    if sandbox is None or sandbox.get("lifecycle_version") != "v1":
        raise ProjectOperationError(404, f"Project '{project_name}' is not registered")
    try:
        volume = docker_client.volumes.get(str(sandbox["volume_name"]))
    except NotFound as error:
        raise ProjectOperationError(
            409,
            f"Managed sandbox '{project_name}' workspace is missing",
        ) from error
    try:
        validate_ownership(volume, sandbox_id=project_name)
    except ValueError as error:
        raise ProjectOperationError(409, str(error)) from error
    lifecycle_status = SandboxLifecycleStatus(
        str(
            sandbox.get("lifecycle_status")
            or SandboxLifecycleStatus.CREATING.value
        )
    )
    return ProjectRegistration(
        sandbox_id=project_name,
        name=project_name,
        source_path=f"managed:{sandbox['project_id']}",
        volume_name=str(sandbox["volume_name"]),
        created_at=str(sandbox.get("created_at") or ""),
        ready=lifecycle_status is SandboxLifecycleStatus.READY,
    )


def ensure_sandbox_registered(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    *,
    project: ProjectRegistration | None = None,
) -> tuple[str, str, ProjectRegistration]:
    """Returns one ready managed sandbox and its remote project id.

    Sandbox creation already writes the row, so the registration below is a
    backstop for a caller that supplies its own `project`.
    """
    project = project or inspect_registered_project(
        docker_client,
        project_name,
        controller_store,
    )
    if not project.ready:
        raise ProjectOperationError(409, f"Project '{project_name}' is not ready")
    sandbox_id = getattr(project, "sandbox_id", "") or hashlib.sha256(
        f"sandbox:{project.volume_name}".encode()
    ).hexdigest()[:32]
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is not None:
        return sandbox_id, str(sandbox["project_id"]), project
    source_path = getattr(project, "source_path", "") or f"managed:{sandbox_id}"
    project_key = managed_project_key(source_path)
    controller_store.register_sandbox(
        sandbox_id=sandbox_id,
        project_id=project_key,
        project_name=project.name,
        source_path=source_path,
        volume_name=project.volume_name,
        status="ready",
        created_at=getattr(project, "created_at", ""),
    )
    return sandbox_id, project_key, project


def ensure_git_baseline(
    docker_client: DockerClient,
    git_image: str,
    volume_name: str,
) -> str:
    """Ensures the sandbox volume has a baseline commit and returns its HEAD."""
    output = run_git(
        docker_client,
        image=git_image,
        volumes={volume_name: {"bind": "/project", "mode": "rw"}},
        script=GIT_BASELINE_SCRIPT,
        ensure_image=True,
    )
    return output.decode().strip().splitlines()[-1]


def project_id(source_path: str) -> str:
    return uuid5(NAMESPACE_URL, f"orchestrator-project:{source_path}").hex


def managed_project_key(source_path: str) -> str:
    """Resolves the remote project id a managed sandbox records.

    `inspect_registered_project` stores the id as `managed:<project id>`, so
    the prefix carries the real key and hashing it would invent a new one.
    Anything unprefixed predates that encoding and keeps the derived id.
    """
    if source_path.startswith("managed:"):
        return source_path.removeprefix("managed:")
    return project_id(source_path)
