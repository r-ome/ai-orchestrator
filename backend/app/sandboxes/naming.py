"""Deterministic names for managed v1 sandbox resources."""

from collections.abc import Mapping
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5


_OWNERSHIP_SANDBOX_ID = "orchestrator.sandbox.id"
_OWNERSHIP_PROJECT_ID = "orchestrator.project.id"
_OWNERSHIP_LIFECYCLE_VERSION = "orchestrator.lifecycle.version"
_MIRROR_LABEL = "orchestrator.project.mirror"
_FEATURE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

# These labels distinguish one sandbox's disposable resources from shared
# infrastructure.  Orphan cleanup treats labels as the authority, never a
# resource name alone.
_SHARED_DATABASE_LABEL = "orchestrator.shared-database"
_PREVIEW_DATA_MANAGED_LABEL = "orchestrator.preview.data-managed"
_PREVIEW_PERSISTENT_LABEL = "orchestrator.preview.persistent"


def validate_feature_key(feature_key: str) -> str:
    if not isinstance(feature_key, str) or not feature_key:
        raise ValueError("feature_key is required")
    if not _FEATURE_KEY_PATTERN.fullmatch(feature_key):
        raise ValueError(
            "feature_key must match ^[a-z0-9][a-z0-9-]{1,63}$"
        )
    return feature_key


def sandbox_id_for(project_id: str, feature_key: str) -> str:
    """Return the stable v1 sandbox identity for one project feature."""
    validate_feature_key(feature_key)
    return uuid5(NAMESPACE_URL, f"{project_id}:{feature_key}").hex


def short_id(sandbox_id: str) -> str:
    return sandbox_id[:12]


# The full hex value is the identity recorded in manifests and ownership labels.
# Short IDs appear only in Docker names, where resource name length limits matter.
def workspace_volume(sandbox_id: str) -> str:
    return f"sbx-{short_id(sandbox_id)}-ws"


def mirror_volume(project_id: str) -> str:
    """Return the deterministic, project-scoped canonical mirror name."""
    return f"prj-{project_id[:12]}-mirror"


def agent_container(sandbox_id: str) -> str:
    return f"sbx-{short_id(sandbox_id)}-agent"


def network(sandbox_id: str) -> str:
    return f"sbx-{short_id(sandbox_id)}-net"


def db_data_volume(sandbox_id: str) -> str:
    return f"sbx-{short_id(sandbox_id)}-db"


def database_name(sandbox_id: str) -> str:
    return f"sbx_{short_id(sandbox_id)}"


def feature_branch(feature_key: str) -> str:
    return f"feature/{validate_feature_key(feature_key)}"


def ownership_labels(*, sandbox_id: str, project_id: str) -> dict[str, str]:
    return {
        _OWNERSHIP_SANDBOX_ID: sandbox_id,
        _OWNERSHIP_PROJECT_ID: project_id,
        _OWNERSHIP_LIFECYCLE_VERSION: "v1",
    }


def mirror_ownership_labels(*, project_id: str) -> dict[str, str]:
    """Labels for shared project infrastructure, never for one sandbox."""
    return {
        _OWNERSHIP_PROJECT_ID: project_id,
        _OWNERSHIP_LIFECYCLE_VERSION: "v1",
        _MIRROR_LABEL: "true",
    }


def validate_ownership(resource: Any, *, sandbox_id: str) -> None:
    """Reject a resource unless its deterministic name and ownership labels agree."""
    name = _resource_name(resource)
    expected_prefix = f"sbx-{short_id(sandbox_id)}-"
    if not name.startswith(expected_prefix):
        raise ValueError(
            f"resource name {name!r} does not belong to sandbox {sandbox_id!r}"
        )

    labels = _resource_labels(resource)
    if labels.get(_OWNERSHIP_SANDBOX_ID) != sandbox_id:
        raise ValueError("resource sandbox ownership label is missing or does not match")
    if not labels.get(_OWNERSHIP_PROJECT_ID):
        raise ValueError("resource project ownership label is missing")
    if labels.get(_OWNERSHIP_LIFECYCLE_VERSION) != "v1":
        raise ValueError("resource lifecycle ownership label is missing or does not match")


def validate_mirror_ownership(resource: Any, *, project_id: str) -> None:
    """Reject a same-named mirror unless it is this project's shared mirror."""
    name = _resource_name(resource)
    expected_name = mirror_volume(project_id)
    if name != expected_name:
        raise ValueError(
            f"resource name {name!r} does not belong to project {project_id!r}"
        )
    labels = _resource_labels(resource)
    if labels.get(_OWNERSHIP_PROJECT_ID) != project_id:
        raise ValueError("resource project ownership label is missing or does not match")
    if labels.get(_OWNERSHIP_LIFECYCLE_VERSION) != "v1":
        raise ValueError("resource lifecycle ownership label is missing or does not match")
    if labels.get(_MIRROR_LABEL) != "true":
        raise ValueError("resource mirror ownership label is missing or does not match")
    if _OWNERSHIP_SANDBOX_ID in labels:
        raise ValueError("project mirror must not carry a sandbox ownership label")


def is_shared_infrastructure(resource: Any) -> bool:
    """Return whether labels reserve a resource for shared infrastructure."""
    labels = _resource_labels(resource)
    return (
        labels.get(_MIRROR_LABEL) == "true"
        or labels.get(_SHARED_DATABASE_LABEL) == "true"
        or (
            labels.get(_PREVIEW_DATA_MANAGED_LABEL) == "true"
            and labels.get(_PREVIEW_PERSISTENT_LABEL) == "true"
        )
    )


def orphan_ownership_sandbox_id(resource: Any) -> str:
    """Validate a removable orphan's ownership labels and return its sandbox id."""
    labels = _resource_labels(resource)
    sandbox_id = labels.get(_OWNERSHIP_SANDBOX_ID)
    if not isinstance(sandbox_id, str) or not sandbox_id:
        raise ValueError("resource sandbox ownership label is missing")
    validate_ownership(resource, sandbox_id=sandbox_id)
    return sandbox_id


def _resource_name(resource: Any) -> str:
    if isinstance(resource, Mapping):
        value = resource.get("name") or resource.get("Name")
    else:
        value = getattr(resource, "name", None)
        if value is None:
            attributes = getattr(resource, "attrs", {})
            if isinstance(attributes, Mapping):
                value = attributes.get("Name")
    if not value:
        raise ValueError("resource has no name")
    return str(value).lstrip("/")


def _resource_labels(resource: Any) -> Mapping[str, Any]:
    if isinstance(resource, Mapping):
        labels = resource.get("labels") or resource.get("Labels")
    else:
        labels = getattr(resource, "labels", None)
        if labels is None:
            attributes = getattr(resource, "attrs", {})
            labels = attributes.get("Labels") if isinstance(attributes, Mapping) else None
            if labels is None and isinstance(attributes, Mapping):
                config = attributes.get("Config", {})
                labels = config.get("Labels") if isinstance(config, Mapping) else None
    return labels if isinstance(labels, Mapping) else {}
