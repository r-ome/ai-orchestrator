import shlex
from typing import Any

from docker.client import DockerClient
from docker.errors import DockerException
from docker.models.containers import Container

from app.containers.hardened import Egress, HardenedContainerSpec, create_hardened
from app.dependency_cache import _data_volume
from app.labels import LABEL_SERVICE
from app.previews.models import PreviewConfiguration, PreviewNetworkAccess
from app.previews.resources import _ensure_preview_image, _run_preview_command


PREVIEW_CONTAINER_PREFIX = "orchestrator-preview-"


def _network(
    docker_client: DockerClient,
    run_id: str,
    labels: dict[str, str],
    access: PreviewNetworkAccess,
) -> Any:
    return docker_client.networks.create(
        f"orchestrator-preview-{run_id[:12]}",
        driver="bridge",
        internal=access is PreviewNetworkAccess.ISOLATED,
        labels=labels,
    )


def _preview_egress(access: PreviewNetworkAccess) -> Egress:
    """Keep internet previews on their existing external bridge."""
    return Egress.PROVIDER if access is PreviewNetworkAccess.INTERNET else Egress.DENIED


def _direct_ports(
    config: PreviewConfiguration,
    host_port: int,
) -> dict[str, tuple[str, int]] | None:
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        return None
    return {f"{config.container_port}/tcp": ("127.0.0.1", host_port)}


def _gateway_proxy(
    docker_client: DockerClient,
    image: str,
    service_network: Any,
    target_service: str,
    target_port: int,
    host_port: int,
    labels: dict[str, str],
    run_id: str,
) -> tuple[Container, Any, Any]:
    _ensure_preview_image(docker_client, image)
    gateway_network = docker_client.networks.create(
        f"orchestrator-preview-{run_id[:12]}-gateway",
        driver="bridge",
        internal=False,
        labels=labels,
    )
    gateway_volume = _data_volume(
        docker_client,
        run_id,
        "gateway-script",
        labels,
        False,
    )
    script = f"#!/bin/sh\nexec nc {shlex.quote(target_service)} {target_port}\n"
    _run_preview_command(
        docker_client,
        image=image,
        command=[
            "sh",
            "-c",
            "set -eu; printf '%s' \"$FORWARD_SCRIPT\" > /proxy/forward; chmod 700 /proxy/forward",
        ],
        environment={"FORWARD_SCRIPT": script},
        volumes={gateway_volume.name: {"bind": "/proxy", "mode": "rw"}},
    )
    gateway: Container | None = None
    try:
        gateway = create_hardened(docker_client, HardenedContainerSpec(
            image=image,
            command=["nc", "-lk", "-p", "8080", "-e", "/proxy/forward"],
            name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-gateway",
            labels={**labels, LABEL_SERVICE: "gateway"},
            volumes={gateway_volume.name: {"bind": "/proxy", "mode": "ro"}},
            network=gateway_network.name,
            ports={"8080/tcp": ("127.0.0.1", host_port)},
            restart_policy={"Name": "no"},
            mem_limit="128m",
            nano_cpus=250_000_000,
            pids_limit=32,
        ))
        service_network.connect(gateway, aliases=["preview-gateway"])
        gateway.start()
    except Exception:
        if gateway is not None:
            try:
                gateway.remove(force=True, v=True)
            except DockerException:
                pass
        try:
            gateway_network.remove()
        except DockerException:
            pass
        try:
            gateway_volume.remove(force=True)
        except DockerException:
            pass
        raise
    return gateway, gateway_network, gateway_volume
