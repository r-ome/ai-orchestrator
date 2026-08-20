import json

from app.controller.store import ControllerStore
from app.sandboxes.publish import PublishError

from .errors import SandboxConflict, SandboxNotFound


def require_v1(
    sandbox: dict[str, object] | None,
    sandbox_id: str,
    refusal: str,
) -> None:
    if sandbox is None:
        raise SandboxNotFound("Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise SandboxConflict(f"Legacy sandbox '{sandbox_id}' {refusal}")


def _required_sync_value(
    sandbox: dict[str, object], field: str, sandbox_id: str
) -> str:
    value = sandbox.get(field)
    if value is None or not str(value):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no {field}; recreate it explicitly to use v1 sync."
        )
    return str(value)


def _required_staleness_value(
    sandbox: dict[str, object], field: str, sandbox_id: str
) -> str:
    value = sandbox.get(field)
    if value is None or not str(value):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no {field}; recreate it explicitly to use v1 staleness."
        )
    return str(value)


def _sync_strategy(store: ControllerStore, sandbox_id: str) -> str:
    """Merge only when the publication table observes an open PR."""
    publication = store.sandbox_publication(sandbox_id)
    if (
        publication is not None
        and publication.get("pr_number") is not None
        and str(publication.get("pr_state") or "").lower() == "open"
    ):
        return "merge"
    return "rebase"


def _base_branch(base_ref: str) -> str:
    prefix = "refs/heads/"
    if not base_ref.startswith(prefix) or not base_ref[len(prefix) :]:
        raise PublishError(
            409, "Sandbox has an invalid base branch for pull request publishing"
        )
    return base_ref[len(prefix) :]


def _json_value(value: object, default: object) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
