"""Typed access to the managed-sandbox manifest columns."""

from dataclasses import dataclass
from typing import Any

from app.controller.store import ControllerStore
from app.sandboxes.naming import validate_feature_key


@dataclass(frozen=True)
class SandboxManifest:
    sandbox_id: str
    lifecycle_version: str | None = None
    feature_key: str | None = None
    feature_title: str | None = None
    desired_state: str | None = None
    lifecycle_status: str | None = None
    operation: str | None = None
    operation_phase: str | None = None
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


_MANIFEST_FIELDS = tuple(
    field for field in SandboxManifest.__dataclass_fields__ if field != "sandbox_id"
)


def read_manifest(store: ControllerStore, sandbox_id: str) -> SandboxManifest | None:
    row = store.sandbox(sandbox_id)
    if row is None:
        return None
    return _manifest_from_row(row)


def write_manifest(store: ControllerStore, manifest: SandboxManifest) -> None:
    """Persist managed lifecycle state without touching legacy import status."""
    if manifest.lifecycle_version == "v1":
        validate_feature_key(manifest.feature_key)
    store.update_sandbox_manifest(
        sandbox_id=manifest.sandbox_id,
        values={field: getattr(manifest, field) for field in _MANIFEST_FIELDS},
    )


def _manifest_from_row(row: dict[str, Any]) -> SandboxManifest:
    values = {field: row.get(field) for field in _MANIFEST_FIELDS}
    values["pr_requested"] = bool(values["pr_requested"])
    return SandboxManifest(sandbox_id=str(row["id"]), **values)
