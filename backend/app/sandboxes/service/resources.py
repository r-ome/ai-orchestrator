from docker.client import DockerClient
from docker.errors import DockerException, NotFound

from app.controller.store import ControllerStore
from app.platform.naming import is_shared_infrastructure, orphan_ownership_sandbox_id
from app.sandboxes.orphans import parse_orphan_resource_key, resource_is_claimed

from .errors import (
    SandboxConflict,
    SandboxNotFound,
    SandboxUnavailable,
    SandboxValidationError,
)


def remove_orphan_resource(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    resource: str,
) -> str:
    """Remove one operator-selected orphan after checking live manifest ownership."""
    try:
        orphan = parse_orphan_resource_key(resource)
        collection = _docker_collection(docker_client, orphan.kind)
    except ValueError as error:
        raise SandboxValidationError(str(error)) from error
    try:
        docker_resource = collection.get(orphan.name)
    except NotFound as error:
        raise SandboxNotFound("Orphan resource not found") from error
    try:
        if is_shared_infrastructure(docker_resource):
            raise ValueError("shared infrastructure cannot be removed as an orphan")
        # A name is only a discovery hint. Removal requires the complete v1
        # ownership-label shape, which prevents deleting an unrelated sbx-* resource.
        orphan_ownership_sandbox_id(docker_resource)
        if resource_is_claimed(controller_store, orphan):
            raise ValueError("resource is now claimed by a sandbox manifest")
        _remove_manifest_resource(docker_resource, orphan.kind)
    except ValueError as error:
        raise SandboxConflict(str(error)) from error
    except DockerException as error:
        raise SandboxUnavailable(str(error)) from error
    return orphan.key


def _docker_collection(docker_client: DockerClient, kind: str) -> object:
    """Return the Docker collection for a supported resource kind."""
    collections = {
        "volume": docker_client.volumes,
        "container": docker_client.containers,
        "network": docker_client.networks,
    }
    try:
        return collections[kind]
    except KeyError as error:
        raise SandboxValidationError(
            f"Unsupported sandbox resource kind: {kind}"
        ) from error


def _remove_manifest_resource(resource: object, kind: str) -> None:
    if kind == "network":
        try:
            resource.reload()  # type: ignore[attr-defined]
            endpoint_ids = list((resource.attrs.get("Containers") or {}).keys())  # type: ignore[attr-defined]
        except DockerException:
            endpoint_ids = []
        for endpoint_id in endpoint_ids:
            try:
                resource.disconnect(endpoint_id, force=True)  # type: ignore[attr-defined]
            except DockerException:
                continue
        resource.remove()  # type: ignore[attr-defined]
        return
    resource.remove(force=True)  # type: ignore[attr-defined]
