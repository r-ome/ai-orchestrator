from docker.client import DockerClient

from app.controller.store import ControllerStore, SandboxAdmissionError
from app.previews.config import get_preview_settings
from app.sandboxes.git import count_mirror_staleness, fetch_canonical_mirror
from app.sandboxes.lifecycle import lifecycle_conflict_detail, project_mirror_lock

from .coercion import _optional_string, _required_staleness_value, require_v1
from .errors import SandboxConflict, SandboxInternalFailure
from .outcomes import StalenessOutcome


def staleness(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
) -> StalenessOutcome:
    """Fetch the shared mirror, then report this sandbox's informational lag."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(
        sandbox,
        sandbox_id,
        "has no canonical mirror or usable base commit; recreate it explicitly to use v1 staleness.",
    )
    assert sandbox is not None
    project = controller_store.project(str(sandbox["project_id"]))
    if project is None or not project.get("mirror_volume"):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no project mirror; recreate it explicitly to use v1.",
        )
    base_ref = _required_staleness_value(sandbox, "base_ref", sandbox_id)
    current_base_commit = _required_staleness_value(
        sandbox, "current_base_commit", sandbox_id
    )
    mirror_name = str(project["mirror_volume"])
    git_image = get_preview_settings().git_image
    fetch_failure_reason: str | None = None

    # Staleness is sandbox read-only. It intentionally takes no lifecycle
    # lease, so an active agent, task, or preview cannot block inspection.
    # It locks only the shared mirror while canonical fetch mutates its refs.
    try:
        with project_mirror_lock(controller_store, str(project["id"]), "staleness"):
            try:
                fetch_canonical_mirror(
                    docker_client,
                    image=git_image,
                    mirror_volume=mirror_name,
                    ensure_image=True,
                )
            except Exception as error:  # noqa: BLE001 - staleness continues with the last recorded mirror state
                fetch_failure_reason = str(error)
                project = (
                    controller_store.project(str(sandbox["project_id"])) or project
                )
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    if fetch_failure_reason is None:
        project = controller_store.record_v1_project_mirror_fetch(
            project_id=str(project["id"])
        )

    try:
        behind_count = count_mirror_staleness(
            docker_client,
            image=git_image,
            mirror_volume=mirror_name,
            current_base_commit=current_base_commit,
            base_ref=base_ref,
            ensure_image=True,
        )
    except Exception as error:
        if fetch_failure_reason is None:
            raise SandboxInternalFailure(str(error)) from error
        fetch_failure_reason = (
            f"{fetch_failure_reason}; last known mirror state is unavailable: {error}"
        )
        behind_count = None

    return StalenessOutcome(
        behind_count=behind_count,
        base_ref=base_ref,
        current_base_commit=current_base_commit,
        mirror_fetched_at=_optional_string(project.get("mirror_fetched_at")),
        stale_answer=fetch_failure_reason is not None,
        fetch_failure_reason=fetch_failure_reason,
    )
