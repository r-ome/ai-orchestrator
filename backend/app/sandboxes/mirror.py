"""Controller-owned canonical project mirrors and sandbox workspace imports."""

from dataclasses import dataclass

from docker.client import DockerClient
from docker.errors import NotFound

from app.sandboxes.git import GitCredentialSource, clone_mirror_to_workspace, ensure_canonical_mirror, run_git
from app.sandboxes.naming import (
    mirror_ownership_labels,
    mirror_volume,
    ownership_labels,
    validate_mirror_ownership,
    validate_ownership,
    workspace_volume,
)


@dataclass(frozen=True)
class MirrorPin:
    volume_name: str
    default_branch: str
    commit: str


def ensure_project_mirror(
    docker_client: DockerClient,
    *,
    image: str,
    project_id: str,
    remote_url: str,
    credential_source: GitCredentialSource | None = None,
) -> MirrorPin:
    """Get or create this project's shared mirror, then fetch and pin its HEAD."""
    volume_name = mirror_volume(project_id)
    try:
        volume = docker_client.volumes.get(volume_name)
    except NotFound:
        docker_client.volumes.create(
            name=volume_name,
            driver="local",
            labels=mirror_ownership_labels(project_id=project_id),
        )
    else:
        validate_mirror_ownership(volume, project_id=project_id)

    # Phase 5 adds a project mirror lock. Concurrent v1 creates can still race
    # here, which is an accepted Phase 4 gap.
    branch, commit = ensure_canonical_mirror(
        docker_client,
        image=image,
        mirror_volume=volume_name,
        remote_url=remote_url,
        credential_source=credential_source,
        ensure_image=True,
    )
    return MirrorPin(volume_name=volume_name, default_branch=branch, commit=commit)


def ensure_workspace_import(
    docker_client: DockerClient,
    *,
    image: str,
    sandbox_id: str,
    project_id: str,
    mirror: MirrorPin,
    feature_branch: str,
) -> str:
    """Create one labelled workspace and import a branch-pinned mirror clone."""
    volume_name = workspace_volume(sandbox_id)
    try:
        volume = docker_client.volumes.get(volume_name)
    except NotFound:
        docker_client.volumes.create(
            name=volume_name,
            driver="local",
            labels=ownership_labels(sandbox_id=sandbox_id, project_id=project_id),
        )
    else:
        validate_ownership(volume, sandbox_id=sandbox_id)
        # A present workspace belongs to a previous create. Never adopt or
        # overwrite it; retries after a partial clone wait for Phase 5 resume.
        raise RuntimeError("sandbox workspace already exists; refusing to re-clone it")

    clone_mirror_to_workspace(
        docker_client,
        image=image,
        mirror_volume=mirror.volume_name,
        workspace_volume=volume_name,
        base_commit=mirror.commit,
        branch=feature_branch,
        ensure_image=True,
    )
    return volume_name


def validate_workspace_import(
    docker_client: DockerClient,
    *,
    sandbox_id: str,
) -> str:
    """Validate the existing deterministic workspace without touching Git."""
    volume_name = workspace_volume(sandbox_id)
    try:
        volume = docker_client.volumes.get(volume_name)
    except NotFound as error:
        raise RuntimeError("sandbox workspace is missing; use lifecycle resume when available") from error
    validate_ownership(volume, sandbox_id=sandbox_id)
    return volume_name


def verify_workspace_identity(
    docker_client: DockerClient,
    *,
    image: str,
    sandbox_id: str,
    feature_branch: str,
) -> None:
    """Prove an existing workspace is this detached v1 repository and branch."""
    volume_name = validate_workspace_import(docker_client, sandbox_id=sandbox_id)
    run_git(
        docker_client,
        image=image,
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        script=(
            "set -eu\n"
            "git -C /workspace rev-parse --is-inside-work-tree | grep -qx true\n"
            "test -z \"$(git -C /workspace remote)\"\n"
            f"test \"$(git -C /workspace branch --show-current)\" = {feature_branch!r}\n"
        ),
    )


def validate_project_mirror(
    docker_client: DockerClient,
    *,
    project_id: str,
) -> str:
    """Validate the shared mirror on an idempotent create without fetching it."""
    volume_name = mirror_volume(project_id)
    try:
        volume = docker_client.volumes.get(volume_name)
    except NotFound as error:
        raise RuntimeError("project mirror is missing; use lifecycle resume when available") from error
    validate_mirror_ownership(volume, project_id=project_id)
    return volume_name
