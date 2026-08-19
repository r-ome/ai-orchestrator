"""Typed access to the managed-sandbox manifest columns."""

from dataclasses import dataclass, replace
from typing import Any

from app.controller.store import ControllerStore
from app.sandboxes.models import SandboxLifecycleStatus, source_statuses
from app.platform.naming import validate_feature_key


@dataclass(frozen=True)
class SandboxManifest:
    sandbox_id: str
    lifecycle_version: str | None = None
    feature_key: str | None = None
    feature_title: str | None = None
    desired_state: str | None = None
    lifecycle_status: SandboxLifecycleStatus | None = None
    last_error: str | None = None
    base_ref: str | None = None
    created_base_commit: str | None = None
    current_base_commit: str | None = None
    pending_base_commit: str | None = None
    feature_branch: str | None = None
    agent_provider: str | None = None
    network_policy: str | None = None
    db_engine: str | None = None
    db_name: str | None = None
    schema_baseline_hash: str | None = None
    db_data_volume: str | None = None
    publish_remote: str | None = None
    remote_branch: str | None = None
    pr_requested: bool = False

    def __post_init__(self) -> None:
        if self.lifecycle_status is not None and not isinstance(
            self.lifecycle_status, SandboxLifecycleStatus
        ):
            object.__setattr__(
                self,
                "lifecycle_status",
                SandboxLifecycleStatus(str(self.lifecycle_status)),
            )


_MANIFEST_FIELDS = tuple(
    field for field in SandboxManifest.__dataclass_fields__ if field != "sandbox_id"
)


def read_manifest(store: ControllerStore, sandbox_id: str) -> SandboxManifest | None:
    row = store.sandbox(sandbox_id)
    if row is None:
        return None
    return _manifest_from_row(row)


def write_manifest(store: ControllerStore, manifest: SandboxManifest) -> bool:
    """Persist manifest fields without changing an established lifecycle status."""
    target = manifest.lifecycle_status
    statuses = (target,) if target is not None else None
    return _write_manifest(store, manifest, from_statuses=statuses)


def transition_sandbox_lifecycle(
    store: ControllerStore,
    manifest: SandboxManifest,
    *,
    to_status: SandboxLifecycleStatus,
) -> bool:
    """The only way a managed sandbox lifecycle status changes.

    Callers name a destination, never a source, so a transition the table does
    not draw cannot be requested. The store turns the derived sources into the
    manifest UPDATE's WHERE clause, keeping the check and write atomic.
    """
    target = replace(manifest, lifecycle_status=to_status)
    statuses = source_statuses(to_status)
    return _write_manifest(store, target, from_statuses=statuses)


def _write_manifest(
    store: ControllerStore,
    manifest: SandboxManifest,
    *,
    from_statuses: frozenset[SandboxLifecycleStatus]
    | tuple[SandboxLifecycleStatus, ...]
    | None,
) -> bool:
    if manifest.lifecycle_version == "v1":
        validate_feature_key(manifest.feature_key)
    return store.update_sandbox_manifest(
        sandbox_id=manifest.sandbox_id,
        values={
            field: (
                getattr(manifest, field).value
                if field == "lifecycle_status"
                and isinstance(getattr(manifest, field), SandboxLifecycleStatus)
                else getattr(manifest, field)
            )
            for field in _MANIFEST_FIELDS
        },
        from_lifecycle_statuses=(
            [status.value for status in from_statuses]
            if from_statuses is not None
            else None
        ),
        allow_unset_lifecycle_status=manifest.lifecycle_status is not None,
    )


def _manifest_from_row(row: dict[str, Any]) -> SandboxManifest:
    values = {field: row.get(field) for field in _MANIFEST_FIELDS}
    lifecycle_status = values["lifecycle_status"]
    values["lifecycle_status"] = (
        SandboxLifecycleStatus(str(lifecycle_status))
        if lifecycle_status is not None
        else None
    )
    values["pr_requested"] = bool(values["pr_requested"])
    return SandboxManifest(sandbox_id=str(row["id"]), **values)
