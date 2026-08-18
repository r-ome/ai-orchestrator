"""Hardened execution for controller-launched Git containers."""

import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Protocol

from docker.client import DockerClient
from docker.errors import ContainerError

from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened
from app.containers.images import ensure_image as ensure_container_image
from app.controller.config import get_controller_settings


class GitNetworkMode(Enum):
    """Network policy for a Git container.

    ``ENABLED`` permits Git credentials to reach Git. Only the canonical
    mirror fetch path may use it.
    """

    NONE = "none"
    ENABLED = "enabled"


_DEFAULT_ENVIRONMENT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/tmp",
}
_HOOKS_DISABLED_ENVIRONMENT = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "/dev/null",
}

GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE = "ORCHESTRATOR_GITHUB_READ_TOKEN"
GITHUB_READ_TOKEN_PATH = "/run/secrets/github_read_token"
GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE = "ORCHESTRATOR_GITHUB_WRITE_TOKEN"
GITHUB_WRITE_TOKEN_PATH = "/run/secrets/github_write_token"


def describe_git_failure(error: Exception) -> str:
    """Return a safe, operator-facing summary of a Git container failure."""
    if not isinstance(error, ContainerError):
        try:
            return str(error)
        except Exception:
            return type(error).__name__

    try:
        stderr = error.stderr
        if isinstance(stderr, bytes):
            output = stderr.decode(errors="replace")
        elif isinstance(stderr, str):
            output = stderr
        else:
            output = ""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return f"Git failed with exit status {_container_error_exit_status(error)}"
        lowered_output = output.lower()
        if "denied to" in lowered_output or "the requested url returned error: 403" in lowered_output:
            repository = _github_repository_from_stderr(output)
            target = f" for {repository}" if repository else ""
            # Quote what GitHub actually said. A 403 is usually a missing scope,
            # but SSO enforcement and branch protection land here too, and each
            # needs a different remedy. Naming only the likeliest one sends an
            # operator down the wrong path.
            reason = _denial_reason(lines)
            return (
                f"GitHub rejected the write token{target}: {reason} "
                "The token usually needs the repo scope (classic) or Contents write "
                "permission (fine-grained)."
            )
        return "; ".join(_without_remote_prefix(line) for line in lines[-3:])[:500]
    except Exception:
        return f"Git failed with exit status {_container_error_exit_status(error)}"


def _container_error_exit_status(error: ContainerError) -> str:
    try:
        return str(error.exit_status)
    except Exception:
        return "unknown"


def _denial_reason(lines: list[str]) -> str:
    """Pick the line that says why, preferring GitHub's own wording."""
    for line in lines:
        stripped = _without_remote_prefix(line)
        if "denied" in stripped.lower():
            return stripped if stripped.endswith(".") else f"{stripped}."
    return "the push was refused."


def _without_remote_prefix(line: str) -> str:
    if line.startswith("remote:"):
        return line.removeprefix("remote:").lstrip()
    return line


def _github_repository_from_stderr(stderr: str) -> str | None:
    patterns = (
        r"Permission to ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) denied",
        r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, stderr, flags=re.IGNORECASE)
        if match:
            return match.group(1).removesuffix(".git")
    return None


_GITHUB_READ_CREDENTIAL_HELPER = (
    "!f() { "
    f'if [ ! -f {GITHUB_READ_TOKEN_PATH} ] || [ ! -s {GITHUB_READ_TOKEN_PATH} ]; then '
    f'echo "GitHub read credential mount failed: expected a non-empty regular file at {GITHUB_READ_TOKEN_PATH}" >&2; '
    "exit 1; "
    "fi; "
    'if [ "$1" = get ]; then '
    f'printf "%s\\n" "username=x-access-token" "password=$(cat {GITHUB_READ_TOKEN_PATH})"; '
    "fi; "
    "}; f"
)
_CREDENTIAL_ENVIRONMENT = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "/dev/null",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": _GITHUB_READ_CREDENTIAL_HELPER,
}
_GITHUB_WRITE_CREDENTIAL_HELPER = (
    "!f() { "
    f'if [ ! -f {GITHUB_WRITE_TOKEN_PATH} ] || [ ! -s {GITHUB_WRITE_TOKEN_PATH} ]; then '
    f'echo "GitHub write credential mount failed: expected a non-empty regular file at {GITHUB_WRITE_TOKEN_PATH}" >&2; '
    "exit 1; "
    "fi; "
    'if [ "$1" = get ]; then '
    f'printf "%s\\n" "username=x-access-token" "password=$(cat {GITHUB_WRITE_TOKEN_PATH})"; '
    "fi; "
    "}; f"
)
# This extends the hook-disabling configuration set by run_git.  Do not set
# this count to one: doing so would silently reactivate repository hooks while
# the container can read a write token.
_WRITE_CREDENTIAL_ENVIRONMENT = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": "/dev/null",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": _GITHUB_WRITE_CREDENTIAL_HELPER,
}
_CREDENTIAL_MOUNT_CHECK = (
    f"if [ ! -f {GITHUB_READ_TOKEN_PATH} ] || [ ! -s {GITHUB_READ_TOKEN_PATH} ]; then\n"
    f'  echo "GitHub read credential mount failed: expected a non-empty regular file at {GITHUB_READ_TOKEN_PATH}" >&2\n'
    "  exit 70\n"
    "fi\n"
)
_WRITE_CREDENTIAL_MOUNT_CHECK = (
    f"if [ ! -f {GITHUB_WRITE_TOKEN_PATH} ] || [ ! -s {GITHUB_WRITE_TOKEN_PATH} ]; then\n"
    f'  echo "GitHub write credential mount failed: expected a non-empty regular file at {GITHUB_WRITE_TOKEN_PATH}" >&2\n'
    "  exit 70\n"
    "fi\n"
)
_CANONICAL_FETCH_SCRIPT = (
    "set -eu\n"
    "git -C /mirror config --replace-all remote.origin.fetch '+refs/*:refs/*'\n"
    "git -C /mirror config --unset-all remote.origin.mirror || true\n"
    "git -C /mirror fetch --prune origin\n"
)
_MIRROR_STALENESS_SCRIPT = (
    "set -eu\n"
    "git -C /mirror rev-list --count "
    "\"$ORCHESTRATOR_CURRENT_BASE_COMMIT..$ORCHESTRATOR_BASE_REF\"\n"
)
_WORKSPACE_CLEAN_SCRIPT = (
    "set -eu\n"
    "test -z \"$(git -C /workspace status --porcelain)\"\n"
)
_CREATE_SAFETY_REF_SCRIPT = (
    "set -eu\n"
    "git -C /workspace update-ref \"$ORCHESTRATOR_SAFETY_REF\" HEAD\n"
)
_MIRROR_BASE_COMMIT_SCRIPT = (
    "set -eu\n"
    "git -C /mirror rev-parse \"$ORCHESTRATOR_BASE_REF\"\n"
)
_SYNC_WORKSPACE_SCRIPT = (
    "set -eu\n"
    "git -C /workspace fetch --no-tags /mirror "
    "\"$ORCHESTRATOR_BASE_REF:$ORCHESTRATOR_BASE_REF\"\n"
    "test \"$(git -C /workspace rev-parse \"$ORCHESTRATOR_BASE_REF\")\" = "
    "\"$ORCHESTRATOR_PENDING_BASE_COMMIT\"\n"
    "if [ \"$ORCHESTRATOR_SYNC_STRATEGY\" = rebase ]; then\n"
    "  git -C /workspace rebase \"$ORCHESTRATOR_BASE_REF\"\n"
    "else\n"
    "  git -C /workspace merge --no-edit \"$ORCHESTRATOR_BASE_REF\"\n"
    "fi\n"
)
_RESTORE_SAFETY_REF_SCRIPT = (
    "set -eu\n"
    "git -C /workspace rebase --abort >/dev/null 2>&1 || true\n"
    "git -C /workspace merge --abort >/dev/null 2>&1 || true\n"
    "git -C /workspace reset --hard \"$ORCHESTRATOR_SAFETY_REF\"\n"
)
_CANONICAL_MIRROR_SCRIPT = (
    "set -eu\n"
    "if [ ! -f /mirror/HEAD ]; then\n"
    "  git clone --mirror -q \"$ORCHESTRATOR_MIRROR_REMOTE\" /mirror\n"
    "  git -C /mirror config --unset-all remote.origin.mirror || true\n"
    "else\n"
    "  git -C /mirror rev-parse --is-bare-repository | grep -qx true\n"
    "  test \"$(git -C /mirror remote get-url origin)\" = \"$ORCHESTRATOR_MIRROR_REMOTE\"\n"
    "  git -C /mirror config --replace-all remote.origin.fetch '+refs/*:refs/*'\n"
    "  git -C /mirror config --unset-all remote.origin.mirror || true\n"
    "  git -C /mirror fetch --prune origin\n"
    "  default_ref=$(git -C /mirror ls-remote --symref origin HEAD "
    "| awk '$1 == \"ref:\" && $3 == \"HEAD\" { print $2; exit }')\n"
    "  test -n \"$default_ref\"\n"
    "  git -C /mirror symbolic-ref HEAD \"$default_ref\"\n"
    "fi\n"
    "branch=$(git -C /mirror symbolic-ref --quiet --short HEAD)\n"
    "commit=$(git -C /mirror rev-parse \"refs/heads/$branch\")\n"
    "printf '%s\\n%s\\n' \"$branch\" \"$commit\"\n"
)
_CLONE_SANDBOX_SCRIPT = (
    "set -eu\n"
    "git clone --no-local /mirror /workspace\n"
    "cd /workspace\n"
    "for remote in $(git remote); do git remote remove \"$remote\"; done\n"
    "test -z \"$(git remote)\"\n"
    "mkdir -p .git/hooks\n"
    "find .git/hooks -mindepth 1 -maxdepth 1 -exec rm -rf {} +\n"
    "test -z \"$(find .git/hooks -mindepth 1 -print -quit)\"\n"
    "mkdir -p .git/info\n"
    "touch .git/info/exclude\n"
    "for scaffold in .agent .claude .orchestrator; do\n"
    "  if ! grep -qxF \"/$scaffold/\" .git/info/exclude; then\n"
    "    printf '/%s/\\n' \"$scaffold\" >> .git/info/exclude\n"
    "  fi\n"
    "done\n"
    "git checkout -q -B \"$ORCHESTRATOR_FEATURE_BRANCH\" \"$ORCHESTRATOR_BASE_COMMIT\"\n"
    "test -z \"$(git remote)\"\n"
)
_PUSH_WORKSPACE_TO_MIRROR_SCRIPT = (
    "set -eu\n"
    "test -z \"$(git -C /workspace remote)\"\n"
    "test \"$(git -C /workspace branch --show-current)\" = \"$ORCHESTRATOR_FEATURE_BRANCH\"\n"
    "test \"$(git -C /workspace rev-parse HEAD)\" = \"$ORCHESTRATOR_REVIEWED_HEAD\"\n"
    "git -C /workspace push /mirror \"HEAD:refs/heads/$ORCHESTRATOR_REMOTE_BRANCH\"\n"
    "git -C /mirror rev-parse \"refs/heads/$ORCHESTRATOR_REMOTE_BRANCH\"\n"
)
_REMOTE_BRANCH_SHA_SCRIPT = (
    "set -eu\n"
    "git -C /mirror ls-remote origin \"refs/heads/$ORCHESTRATOR_REMOTE_BRANCH\" "
    "| awk 'NR == 1 { print $1 }'\n"
)
_PUSH_MIRROR_TO_REMOTE_SCRIPT = (
    "set -eu\n"
    "git -C /mirror push origin "
    "\"refs/heads/$ORCHESTRATOR_REMOTE_BRANCH:refs/heads/$ORCHESTRATOR_REMOTE_BRANCH\"\n"
)
_PUSH_MIRROR_TO_REMOTE_FORCE_WITH_LEASE_SCRIPT = (
    "set -eu\n"
    "git -C /mirror push --force-with-lease origin "
    "\"refs/heads/$ORCHESTRATOR_REMOTE_BRANCH:refs/heads/$ORCHESTRATOR_REMOTE_BRANCH\"\n"
)


class GitCredentialSource(Protocol):
    """Provides the controller's GitHub read token for one fetch."""

    def read_token(self) -> str | None:
        """Return a token, or ``None`` when no read credential is configured."""


class EnvironmentGitCredentialSource:
    """Reads the GitHub read token from the controller process environment."""

    def read_token(self) -> str | None:
        return os.environ.get(GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE)


class GitWriteCredentialSource(Protocol):
    """Provides the controller's GitHub write token for one publish."""

    def write_token(self) -> str | None:
        """Return a token, or ``None`` when no write credential is configured."""


class EnvironmentGitWriteCredentialSource:
    """Reads the GitHub write token from the controller process environment."""

    def write_token(self) -> str | None:
        return os.environ.get(GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE)


class GitTimeoutError(RuntimeError):
    """A Git container passed its deadline and was killed."""


class GitCredentialError(RuntimeError):
    """Raised when a controller Git credential cannot be used safely."""


@contextmanager
def provision_git_read_token(
    source: GitCredentialSource,
    secret_directory: Path,
) -> Iterator[Path]:
    """Write one read token to a private, short-lived host file.

    The caller supplies a Docker-shared directory. Each invocation creates a
    private child directory so both the directory and the token file have the
    permissions required for a bind-mounted secret.
    """
    token = source.read_token()
    if not token:
        raise GitCredentialError(
            f"GitHub read credentials are not configured. Set "
            f"{GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE} in the controller environment."
        )

    root = secret_directory.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    fetch_directory = Path(tempfile.mkdtemp(prefix="github-read-", dir=root))
    os.chmod(fetch_directory, 0o700)
    secret_path = fetch_directory / "github_read_token"
    try:
        file_descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(token)
        os.chmod(secret_path, 0o600)
        yield secret_path
    finally:
        secret_path.unlink(missing_ok=True)
        shutil.rmtree(fetch_directory, ignore_errors=True)


@contextmanager
def provision_git_write_token(
    source: GitWriteCredentialSource,
    secret_directory: Path,
) -> Iterator[Path]:
    """Write one publish-only token to a private, short-lived host file.

    This stays separate from ``provision_git_read_token``. A controller with
    only read credentials must keep working for create, sync, and staleness,
    while publish alone gains the capability to mutate the remote.
    """
    token = source.write_token()
    if not token:
        raise GitCredentialError(
            f"GitHub write credentials are not configured. Set "
            f"{GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE} in the controller environment."
        )

    root = secret_directory.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    publish_directory = Path(tempfile.mkdtemp(prefix="github-write-", dir=root))
    os.chmod(publish_directory, 0o700)
    secret_path = publish_directory / "github_write_token"
    try:
        file_descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(token)
        os.chmod(secret_path, 0o600)
        yield secret_path
    finally:
        secret_path.unlink(missing_ok=True)
        shutil.rmtree(publish_directory, ignore_errors=True)


def run_git(
    docker_client: DockerClient,
    *,
    image: str,
    volumes: Mapping[str, Mapping[str, str]],
    script: str,
    network: GitNetworkMode = GitNetworkMode.NONE,
    environment: Mapping[str, str] | None = None,
    ensure_image: bool = False,
    credential_source: GitCredentialSource | None = None,
) -> bytes:
    """Run a Git script in a throwaway hardened container.

    The default has no network. Callers must explicitly pass
    ``GitNetworkMode.ENABLED`` before Git can reach a credential over the
    network.
    """
    if not isinstance(network, GitNetworkMode):
        raise TypeError("network must be a GitNetworkMode")
    if credential_source is not None and network is not GitNetworkMode.ENABLED:
        raise GitCredentialError(
            "GitHub read credentials require GitNetworkMode.ENABLED explicitly"
        )
    if ensure_image:
        ensure_container_image(docker_client, image)

    resolved_environment = dict(environment or {})
    resolved_environment.update(_DEFAULT_ENVIRONMENT)
    # Set this after caller values so every Git invocation disables repository
    # hooks, including scripts added in the future.
    resolved_environment.update(_HOOKS_DISABLED_ENVIRONMENT)
    if credential_source is None:
        output = _run_container(
            docker_client,
            image=image,
            volumes=volumes,
            script=script,
            network=network,
            environment=resolved_environment,
        )
    else:
        secret_directory = get_controller_settings().git_secret_directory
        with provision_git_read_token(credential_source, secret_directory) as secret_path:
            resolved_environment.update(_CREDENTIAL_ENVIRONMENT)
            credential_volumes = dict(volumes)
            credential_volumes[str(secret_path)] = {
                "bind": GITHUB_READ_TOKEN_PATH,
                "mode": "ro",
            }
            output = _run_container(
                docker_client,
                image=image,
                volumes=credential_volumes,
                script=_CREDENTIAL_MOUNT_CHECK + script,
                network=network,
                environment=resolved_environment,
            )
    return output if isinstance(output, bytes) else bytes(output)


def run_git_with_write_credentials(
    docker_client: DockerClient,
    *,
    image: str,
    volumes: Mapping[str, Mapping[str, str]],
    script: str,
    environment: Mapping[str, str] | None = None,
    ensure_image: bool = False,
    credential_source: GitWriteCredentialSource | None = None,
) -> bytes:
    """Run the controller's networked Git publish container.

    This is intentionally not an option on ``run_git``. The read and write
    token paths are separate capability boundaries, even when deployment uses
    one token value for both environment variables.
    """
    if ensure_image:
        ensure_container_image(docker_client, image)
    resolved_environment = dict(environment or {})
    resolved_environment.update(_DEFAULT_ENVIRONMENT)
    resolved_environment.update(_HOOKS_DISABLED_ENVIRONMENT)
    source = credential_source or EnvironmentGitWriteCredentialSource()
    secret_directory = get_controller_settings().git_secret_directory
    with provision_git_write_token(source, secret_directory) as secret_path:
        # Extend the existing hook setting. GIT_CONFIG_COUNT must remain two.
        resolved_environment.update(_WRITE_CREDENTIAL_ENVIRONMENT)
        credential_volumes = dict(volumes)
        credential_volumes[str(secret_path)] = {
            "bind": GITHUB_WRITE_TOKEN_PATH,
            "mode": "ro",
        }
        output = _run_container(
            docker_client,
            image=image,
            volumes=credential_volumes,
            script=_WRITE_CREDENTIAL_MOUNT_CHECK + script,
            network=GitNetworkMode.ENABLED,
            environment=resolved_environment,
        )
    return output if isinstance(output, bytes) else bytes(output)


def fetch_canonical_mirror(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    credential_source: GitCredentialSource | None = None,
    ensure_image: bool = False,
) -> bytes:
    """Fetch a controller-owned mirror with the controller read credential.

    This is the only network-enabled Git primitive. It mounts the mirror and
    the ephemeral read-secret file; it never mounts a workspace or credential
    volume.
    """
    return run_git(
        docker_client,
        image=image,
        volumes={mirror_volume: {"bind": "/mirror", "mode": "rw"}},
        script=_CANONICAL_FETCH_SCRIPT,
        network=GitNetworkMode.ENABLED,
        ensure_image=ensure_image,
        credential_source=credential_source or EnvironmentGitCredentialSource(),
    )


def count_mirror_staleness(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    current_base_commit: str,
    base_ref: str,
    ensure_image: bool = False,
) -> int:
    """Count commits in the mirror that the sandbox has not imported.

    This has no network or credential path. Callers fetch canonically first.
    """
    output = run_git(
        docker_client,
        image=image,
        volumes={mirror_volume: {"bind": "/mirror", "mode": "ro"}},
        script=_MIRROR_STALENESS_SCRIPT,
        environment={
            "ORCHESTRATOR_CURRENT_BASE_COMMIT": current_base_commit,
            "ORCHESTRATOR_BASE_REF": base_ref,
        },
        ensure_image=ensure_image,
    )
    result = output.decode().strip()
    if not result.isdigit():
        raise RuntimeError(f"mirror staleness command returned invalid count: {result!r}")
    return int(result)


def require_clean_workspace(
    docker_client: DockerClient,
    *,
    image: str,
    workspace_volume: str,
    ensure_image: bool = False,
) -> None:
    """Refuse sync before it creates a ref, fetches, or changes the workspace."""
    run_git(
        docker_client,
        image=image,
        volumes={workspace_volume: {"bind": "/workspace", "mode": "rw"}},
        script=_WORKSPACE_CLEAN_SCRIPT,
        ensure_image=ensure_image,
    )


def create_workspace_safety_ref(
    docker_client: DockerClient,
    *,
    image: str,
    workspace_volume: str,
    safety_ref: str,
    ensure_image: bool = False,
) -> None:
    """Pin the current workspace HEAD in a controller-named local ref."""
    run_git(
        docker_client,
        image=image,
        volumes={workspace_volume: {"bind": "/workspace", "mode": "rw"}},
        script=_CREATE_SAFETY_REF_SCRIPT,
        environment={"ORCHESTRATOR_SAFETY_REF": safety_ref},
        ensure_image=ensure_image,
    )


def mirror_base_commit(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    base_ref: str,
    ensure_image: bool = False,
) -> str:
    """Read the fetched base ref from the local, credential-free mirror."""
    output = run_git(
        docker_client,
        image=image,
        volumes={mirror_volume: {"bind": "/mirror", "mode": "ro"}},
        script=_MIRROR_BASE_COMMIT_SCRIPT,
        environment={"ORCHESTRATOR_BASE_REF": base_ref},
        ensure_image=ensure_image,
    )
    commit = output.decode().strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit.lower()):
        raise RuntimeError(f"mirror base ref did not resolve to a commit: {commit!r}")
    return commit


def sync_workspace_from_mirror(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    workspace_volume: str,
    base_ref: str,
    pending_base_commit: str,
    strategy: str,
    ensure_image: bool = False,
) -> None:
    """Fetch only from the local mirror, then rebase or merge the workspace."""
    if strategy not in {"rebase", "merge"}:
        raise ValueError("sync strategy must be rebase or merge")
    run_git(
        docker_client,
        image=image,
        volumes={
            mirror_volume: {"bind": "/mirror", "mode": "ro"},
            workspace_volume: {"bind": "/workspace", "mode": "rw"},
        },
        script=_SYNC_WORKSPACE_SCRIPT,
        environment={
            "ORCHESTRATOR_BASE_REF": base_ref,
            "ORCHESTRATOR_PENDING_BASE_COMMIT": pending_base_commit,
            "ORCHESTRATOR_SYNC_STRATEGY": strategy,
        },
        ensure_image=ensure_image,
    )


def restore_workspace_safety_ref(
    docker_client: DockerClient,
    *,
    image: str,
    workspace_volume: str,
    safety_ref: str,
    ensure_image: bool = False,
) -> None:
    """Restore Git from a safety ref after a rebase or merge failure only."""
    run_git(
        docker_client,
        image=image,
        volumes={workspace_volume: {"bind": "/workspace", "mode": "rw"}},
        script=_RESTORE_SAFETY_REF_SCRIPT,
        environment={"ORCHESTRATOR_SAFETY_REF": safety_ref},
        ensure_image=ensure_image,
    )


def ensure_canonical_mirror(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    remote_url: str,
    credential_source: GitCredentialSource | None = None,
    ensure_image: bool = False,
) -> tuple[str, str]:
    """Create or validate a bare mirror, fetch it, and return branch and HEAD.

    This is deliberately the only network-enabled operation used by v1 create.
    A second call validates the existing bare repository before fetching it; it
    never reclones or replaces the project mirror.
    """
    output = run_git(
        docker_client,
        image=image,
        volumes={mirror_volume: {"bind": "/mirror", "mode": "rw"}},
        script=_CANONICAL_MIRROR_SCRIPT,
        network=GitNetworkMode.ENABLED,
        environment={"ORCHESTRATOR_MIRROR_REMOTE": remote_url},
        ensure_image=ensure_image,
        credential_source=credential_source or EnvironmentGitCredentialSource(),
    )
    lines = output.decode().strip().splitlines()
    if len(lines) < 2 or not lines[-2] or not lines[-1]:
        raise RuntimeError("canonical mirror did not report a default branch and commit")
    return lines[-2], lines[-1]


def clone_mirror_to_workspace(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    workspace_volume: str,
    base_commit: str,
    branch: str,
    ensure_image: bool = False,
) -> bytes:
    """Make a remote-free, hook-free independent clone from a local mirror."""
    return run_git(
        docker_client,
        image=image,
        volumes={
            mirror_volume: {"bind": "/mirror", "mode": "ro"},
            workspace_volume: {"bind": "/workspace", "mode": "rw"},
        },
        script=_CLONE_SANDBOX_SCRIPT,
        environment={
            "ORCHESTRATOR_BASE_COMMIT": base_commit,
            "ORCHESTRATOR_FEATURE_BRANCH": branch,
        },
        ensure_image=ensure_image,
    )


def push_workspace_to_mirror(
    docker_client: DockerClient,
    *,
    image: str,
    workspace_volume: str,
    mirror_volume: str,
    feature_branch: str,
    remote_branch: str,
    reviewed_head: str,
    ensure_image: bool = False,
) -> str:
    """Copy the reviewed workspace ref into the project's mirror volume only.

    The mirror is local to this machine. Nothing here reaches the Git remote;
    publishing is a separate, credentialed step.
    """
    output = run_git(
        docker_client,
        image=image,
        volumes={
            workspace_volume: {"bind": "/workspace", "mode": "ro"},
            mirror_volume: {"bind": "/mirror", "mode": "rw"},
        },
        script=_PUSH_WORKSPACE_TO_MIRROR_SCRIPT,
        environment={
            "ORCHESTRATOR_FEATURE_BRANCH": feature_branch,
            "ORCHESTRATOR_REMOTE_BRANCH": remote_branch,
            "ORCHESTRATOR_REVIEWED_HEAD": reviewed_head,
        },
        ensure_image=ensure_image,
    )
    return _commit_from_output(output, "workspace-to-mirror push")


def assert_workspace_has_no_remotes(
    docker_client: DockerClient,
    *,
    image: str,
    workspace_volume: str,
    ensure_image: bool = False,
) -> None:
    """Assert the sandbox remains disconnected from every remote."""
    run_git(
        docker_client,
        image=image,
        volumes={workspace_volume: {"bind": "/workspace", "mode": "ro"}},
        script='set -eu\ntest -z "$(git -C /workspace remote)"\n',
        ensure_image=ensure_image,
    )


def remote_branch_sha(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    remote_branch: str,
    credential_source: GitWriteCredentialSource | None = None,
    ensure_image: bool = False,
) -> str | None:
    """Read one remote branch through the controller's write path."""
    output = run_git_with_write_credentials(
        docker_client,
        image=image,
        volumes={mirror_volume: {"bind": "/mirror", "mode": "rw"}},
        script=_REMOTE_BRANCH_SHA_SCRIPT,
        environment={"ORCHESTRATOR_REMOTE_BRANCH": remote_branch},
        ensure_image=ensure_image,
        credential_source=credential_source,
    )
    value = output.decode().strip()
    if not value:
        return None
    return _commit_from_output(output, "remote branch query")


def push_mirror_to_remote(
    docker_client: DockerClient,
    *,
    image: str,
    mirror_volume: str,
    remote_branch: str,
    credential_source: GitWriteCredentialSource | None = None,
    force_with_lease: bool = False,
    ensure_image: bool = False,
) -> None:
    """Publish an already-local mirror ref through the write-only path.

    ``force_with_lease`` is a marked Phase 8 pre-PR extension point. The
    caller must establish that no observed PR exists before selecting it.
    Plain ``--force`` is never available from this module.
    """
    script = (
        _PUSH_MIRROR_TO_REMOTE_FORCE_WITH_LEASE_SCRIPT
        if force_with_lease
        else _PUSH_MIRROR_TO_REMOTE_SCRIPT
    )
    run_git_with_write_credentials(
        docker_client,
        image=image,
        volumes={mirror_volume: {"bind": "/mirror", "mode": "rw"}},
        script=script,
        environment={"ORCHESTRATOR_REMOTE_BRANCH": remote_branch},
        ensure_image=ensure_image,
        credential_source=credential_source,
    )


def _commit_from_output(output: bytes, operation: str) -> str:
    commit = output.decode().strip().splitlines()[-1] if output.strip() else ""
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit.lower()):
        raise RuntimeError(f"{operation} did not report a commit: {commit!r}")
    return commit


def _run_container(
    docker_client: DockerClient,
    *,
    image: str,
    volumes: Mapping[str, Mapping[str, str]],
    script: str,
    network: GitNetworkMode,
    environment: Mapping[str, str],
) -> bytes:
    command = [script]
    result = run_hardened(
        docker_client,
        HardenedRunSpec(
            image=image,
            entrypoint=["sh", "-c"],
            command=command,
            egress=(
                Egress.PROVIDER if network is GitNetworkMode.ENABLED else Egress.DENIED
            ),
            environment=dict(environment),
            volumes=dict(volumes),
            # `/git` is a tmpfs because `alpine/git` declares `VOLUME /git` in its
            # Dockerfile. Docker creates an anonymous volume for any declared VOLUME
            # that a run does not mount over, and `--rm` does not always reap them,
            # so every controller git call used to leak one empty volume. Measured:
            # 4,423 empty anonymous volumes had accumulated on one developer machine.
            # Mounting anything at the path stops the anonymous volume being created.
            tmpfs_size="32m",
            extra_tmpfs={"/git": "rw,nosuid,size=1m"},
            timeout_seconds=get_controller_settings().git_timeout_seconds,
            max_log_bytes=1_048_576,
            capture=Capture.SEPARATE,
        ),
    )
    if result.timed_out:
        # Without this the kill leaves exit_code None, and the ContainerError
        # below would report a timeout as "non-zero exit status None".
        raise GitTimeoutError(
            f"Git container exceeded {get_controller_settings().git_timeout_seconds} seconds"
        )
    if result.exit_code != 0:
        raise ContainerError(None, result.exit_code, command, image, result.stderr)
    return result.stdout.encode("utf-8")
