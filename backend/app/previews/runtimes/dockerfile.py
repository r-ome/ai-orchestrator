import io
from typing import Any

from docker.client import DockerClient
from docker.errors import APIError, BuildError

from app.containers.hardened import HardenedContainerSpec, Rootfs, create_hardened
from app.controller.store import ControllerStore
from app.labels import LABEL_SANDBOX_ID, LABEL_SERVICE
from app.previews._shared import _safe_relative_path
from app.previews.config import PreviewSettings
from app.previews.errors import PreviewOperationError
from app.previews.models import PreviewConfiguration, PreviewNetworkAccess
from app.previews.network import (
    PREVIEW_CONTAINER_PREFIX,
    _direct_ports,
    _gateway_proxy,
    _network,
    _preview_egress,
)
from app.previews.progress import ProgressReporter, _ignore_progress
from app.previews.resources import _validate_built_image
from app.previews.runtimes.environment import _secret_environment
from app.previews.sharing import (
    _connect_sandbox_database_endpoint,
    _managed_preview_database,
)
from app.dependency_cache import _volume_context_tar


def _start_dockerfile(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
    host_port: int,
    progress: ProgressReporter | None = None,
    secrets: dict[str, str] | None = None,
    controller_store: ControllerStore | None = None,
) -> dict[str, Any]:
    report = progress or _ignore_progress
    application_environment = _secret_environment(config, secrets or {})
    managed_database = _managed_preview_database(
        docker_client,
        controller_store,
        labels[LABEL_SANDBOX_ID],
    )
    if managed_database is not None:
        application_environment.update(managed_database.environment)
    report("build-context", "Exporting the current sandbox as a Docker build context")
    context = _volume_context_tar(
        docker_client,
        project_volume,
        ".",
        settings.inspection_image,
    )
    tag = f"orchestrator-preview:{run_id}"
    dockerfile = _safe_relative_path(config.dockerfile, field="dockerfile")
    report("build", f"Building image from {dockerfile}")
    try:
        built_image, _ = docker_client.images.build(
            fileobj=io.BytesIO(context),
            custom_context=True,
            dockerfile=dockerfile,
            tag=tag,
            rm=True,
            forcerm=True,
            labels=labels,
            timeout=settings.build_timeout_seconds,
        )
        _validate_built_image(built_image, settings)
    except (BuildError, APIError) as error:
        raise PreviewOperationError(422, f"Dockerfile build failed: {error}") from error
    report("build", "Docker image build completed")
    report("network", f"Creating {config.network_access.value} preview network")
    network = _network(docker_client, run_id, labels, config.network_access)
    report("container", "Creating application container")
    container = create_hardened(docker_client, HardenedContainerSpec(
        image=tag,
        name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-app",
        rootfs=Rootfs.WRITABLE,
        labels={**labels, LABEL_SERVICE: "app"},
        environment=application_environment,
        volumes={
            project_volume: {"bind": "/sandbox", "mode": "ro"},
            **(managed_database.volumes if managed_database is not None else {}),
        },
        tmpfs_size="256m",
        network=network.name,
        egress=_preview_egress(config.network_access),
        ports=_direct_ports(config, host_port),
        restart_policy={"Name": "no"},
        mem_limit=settings.preview_memory,
        nano_cpus=1_000_000_000,
        pids_limit=256,
    ))
    network.disconnect(container)
    network.connect(container, aliases=["app"])
    if managed_database is not None and managed_database.engine != "sqlite":
        _connect_sandbox_database_endpoint(docker_client, managed_database, container)
    container.start()
    report("container", "Application container started")
    containers = [container]
    networks = [network]
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        report("gateway", "Creating the loopback preview gateway")
        gateway, gateway_network, gateway_volume = _gateway_proxy(
            docker_client,
            settings.inspection_image,
            network,
            "app",
            config.container_port,
            host_port,
            labels,
            run_id,
        )
        containers.append(gateway)
        networks.append(gateway_network)
        data_volumes = [gateway_volume]
        report("gateway", "Loopback preview gateway started")
    else:
        data_volumes = []
    return {
        "containers": containers,
        "networks": networks,
        "volumes": data_volumes,
        "images": [built_image],
        "borrowed_networks": (
            [docker_client.networks.get(managed_database.network_name)]
            if managed_database is not None and managed_database.engine != "sqlite"
            else []
        ),
    }
