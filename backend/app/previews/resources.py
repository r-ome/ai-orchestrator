import base64
from typing import Any

from docker.client import DockerClient
from docker.errors import ContainerError, DockerException, NotFound
from docker.models.containers import Container

from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened
from app.containers.images import ensure_image
from app.platform.labels import (
    LABEL_CONTROLLER_MANAGED,
    LABEL_DATA_MANAGED,
    LABEL_EXPIRES_AT,
    LABEL_KIND,
    LABEL_MANAGED,
    LABEL_PERSISTENT,
    LABEL_RUN_ID,
    LABEL_SANDBOX_ID,
    LABEL_SERVICE,
)
from app.platform.naming import network as sandbox_network_name
from app.previews.config import PreviewSettings
from app.previews.errors import PreviewOperationError

PREVIEW_COMMAND_TIMEOUT_SECONDS = 60
PREVIEW_COMMAND_MAX_LOG_BYTES = 1_048_576


def _ensure_preview_image(docker_client: DockerClient, image: str) -> None:
    try:
        ensure_image(docker_client, image)
    except DockerException as error:
        raise PreviewOperationError(424, f"Preview image '{image}' is unavailable") from error


def _run_preview_command(
    docker_client: DockerClient,
    *,
    image: str,
    command: list[str],
    environment: dict[str, str] | None = None,
    volumes: dict[str, Any] | None = None,
    tmpfs_size: str = "256m",
    max_log_bytes: int = PREVIEW_COMMAND_MAX_LOG_BYTES,
) -> str:
    """Run one isolated preview helper and retain Docker's failed-run error."""
    result = run_hardened(
        docker_client,
        HardenedRunSpec(
            image=image,
            command=command,
            environment=environment or {},
            volumes=volumes or {},
            egress=Egress.DENIED,
            tmpfs_size=tmpfs_size,
            capture=Capture.SEPARATE,
            timeout_seconds=PREVIEW_COMMAND_TIMEOUT_SECONDS,
            max_log_bytes=max_log_bytes,
        ),
    )
    if result.timed_out:
        raise PreviewOperationError(
            408,
            f"Preview helper command exceeded {PREVIEW_COMMAND_TIMEOUT_SECONDS} seconds",
        )
    if result.exit_code != 0:
        raise ContainerError(None, result.exit_code, command, image, result.stderr)
    return result.stdout


def _decode_preview_archive(output: str) -> bytes:
    try:
        return base64.b64decode(output, validate=True)
    except ValueError as error:
        raise PreviewOperationError(502, "Sandbox inspection returned invalid data") from error


def _validate_built_image(image: Any, settings: PreviewSettings) -> None:
    image.reload()
    size = int(image.attrs.get("Size") or 0)
    if size <= settings.maximum_built_image_bytes:
        return
    try:
        image.remove(force=True)
    except DockerException:
        pass
    raise PreviewOperationError(
        422,
        f"Built preview image exceeds {settings.maximum_built_image_bytes} bytes",
    )


def _existing_volume(docker_client: DockerClient, name: str) -> Any | None:
    try:
        return docker_client.volumes.get(name)
    except NotFound:
        return None


def _labels(
    sandbox_id: str,
    run_id: str,
    expires_at: str | None,
) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_SANDBOX_ID: sandbox_id,
        LABEL_RUN_ID: run_id,
        LABEL_KIND: "preview",
        LABEL_EXPIRES_AT: expires_at or "",
    }


def _preview_containers(
    docker_client: DockerClient,
    run_id: str,
    *,
    all: bool,
) -> list[Container]:
    return docker_client.containers.list(
        all=all,
        filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]},
    )


def _preview_networks(docker_client: DockerClient, run_id: str) -> list[Any]:
    return docker_client.networks.list(
        filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]}
    )


def _preview_volumes(docker_client: DockerClient, run_id: str) -> list[Any]:
    return docker_client.volumes.list(
        filters={
            "label": [f"{LABEL_DATA_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]
        }
    )


def _preview_images(docker_client: DockerClient, run_id: str) -> list[Any]:
    return docker_client.images.list(
        filters={
            "label": [f"{LABEL_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]
        }
    )


def _resources_for_run(docker_client: DockerClient, run_id: str) -> dict[str, Any]:
    containers = _preview_containers(docker_client, run_id, all=True)
    borrowed_networks: list[Any] = []
    if containers:
        labels = (containers[0].attrs.get("Config") or {}).get("Labels") or {}
        sandbox_id = str(labels.get(LABEL_SANDBOX_ID) or "")
        if sandbox_id:
            try:
                borrowed_networks.append(
                    docker_client.networks.get(sandbox_network_name(sandbox_id))
                )
            except NotFound:
                pass
    return {
        "containers": containers,
        "networks": _preview_networks(docker_client, run_id),
        "volumes": _preview_volumes(docker_client, run_id),
        "images": _preview_images(docker_client, run_id),
        "borrowed_networks": borrowed_networks,
    }


def _disconnect_foreign_endpoints(network: Any) -> None:
    """Detaches containers the run does not own, such as a shared database.

    Docker refuses to remove a network that still has endpoints, and the shared
    server outlives the run, so it is disconnected instead of removed.
    """
    try:
        network.reload()
    except DockerException:
        return
    for container_id in (network.attrs.get("Containers") or {}):
        try:
            network.disconnect(container_id, force=True)
        except DockerException:
            continue


def _remove_resources(
    resources: dict[str, Any],
    *,
    remove_data_volumes: bool,
) -> dict[str, int]:
    counts = {"containers": 0, "networks": 0, "volumes": 0, "images": 0}
    for network in resources.get("borrowed_networks") or []:
        for container in resources.get("containers") or []:
            try:
                network.disconnect(container, force=True)
            except DockerException:
                continue
    for container in resources.get("containers") or []:
        try:
            # Docker removes anonymous volumes only; named sandbox and mirror volumes survive.
            container.remove(force=True, v=True)
            counts["containers"] += 1
        except NotFound:
            counts["containers"] += 1
        except DockerException:
            continue
    for network in resources.get("networks") or []:
        _disconnect_foreign_endpoints(network)
        try:
            network.remove()
            counts["networks"] += 1
        except NotFound:
            counts["networks"] += 1
        except DockerException:
            continue
    for volume in resources.get("volumes") or []:
        labels = volume.attrs.get("Labels") or {}
        if labels.get(LABEL_PERSISTENT) == "true":
            continue
        database_data = labels.get(LABEL_SERVICE) in {
            "database",
            "database-credentials",
        }
        if not remove_data_volumes and not database_data:
            continue
        try:
            volume.remove(force=True)
            counts["volumes"] += 1
        except NotFound:
            counts["volumes"] += 1
        except DockerException:
            continue
    for image in resources.get("images") or []:
        try:
            image.remove(force=True)
            counts["images"] += 1
        except (AttributeError, NotFound):
            try:
                image_id = getattr(image, "id", str(image))
                image.client.images.remove(image=image_id, force=True)
                counts["images"] += 1
            except (AttributeError, DockerException):
                continue
        except DockerException:
            continue
    return counts
