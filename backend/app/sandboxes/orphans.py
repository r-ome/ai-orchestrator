"""Read-only orphan discovery for managed sandbox Docker resources."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from docker.errors import DockerException

from app.controller.store import ControllerStore
from app.sandboxes.naming import is_shared_infrastructure


RESOURCE_KINDS = ("volume", "container", "network")


@dataclass(frozen=True)
class OrphanResource:
    kind: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


def manifest_resource_claims(store: ControllerStore) -> set[tuple[str, str]]:
    """Return resources claimed by live manifests, excluding tombstones."""
    claimed: set[tuple[str, str]] = set()
    for sandbox in store.sandboxes():
        sandbox_id = str(sandbox["id"])
        volume_name = sandbox.get("volume_name")
        if volume_name:
            claimed.add(("volume", str(volume_name)))
        db_data_volume = sandbox.get("db_data_volume")
        if db_data_volume:
            claimed.add(("volume", str(db_data_volume)))
        for resource in store.sandbox_resources(sandbox_id):
            claimed.add((resource["kind"], resource["name"]))
    return claimed


def discover_orphans(
    docker_client: Any,
    store: ControllerStore,
) -> tuple[list[OrphanResource], int]:
    """Find unclaimed ``sbx-*`` resources without changing Docker state.

    Docker lists run independently. A failure in one collection therefore
    leaves findings from the other collections available to startup reporting.
    The second return value is the number of collection-list failures.
    """
    claimed = manifest_resource_claims(store)
    found: list[OrphanResource] = []
    failures = 0
    for kind, resources in _docker_resources(docker_client):
        try:
            items = resources()
        except DockerException:
            failures += 1
            continue
        for resource in items:
            name = _resource_name(resource)
            if not name.startswith("sbx-"):
                continue
            if (kind, name) in claimed or is_shared_infrastructure(resource):
                continue
            found.append(OrphanResource(kind=kind, name=name))
    return found, failures


def parse_orphan_resource_key(resource: str) -> OrphanResource:
    """Parse the route's stable ``kind:name`` resource key."""
    kind, separator, name = resource.partition(":")
    if separator != ":" or kind not in RESOURCE_KINDS or not name:
        raise ValueError("resource must use volume:, container:, or network: followed by its name")
    return OrphanResource(kind=kind, name=name)


def resource_is_claimed(store: ControllerStore, resource: OrphanResource) -> bool:
    """Recompute ownership immediately before an operator removal."""
    return (resource.kind, resource.name) in manifest_resource_claims(store)


def _docker_resources(docker_client: Any) -> Iterable[tuple[str, Any]]:
    return (
        ("volume", lambda: docker_client.volumes.list()),
        ("container", lambda: docker_client.containers.list(all=True)),
        ("network", lambda: docker_client.networks.list()),
    )


def _resource_name(resource: Any) -> str:
    name = getattr(resource, "name", None)
    if name is None:
        attrs = getattr(resource, "attrs", {})
        if isinstance(attrs, dict):
            name = attrs.get("Name")
    return str(name or "").lstrip("/")
