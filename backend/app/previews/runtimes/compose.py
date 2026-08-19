import io
from typing import Any

import yaml
from docker.client import DockerClient
from docker.errors import APIError, BuildError
from docker.models.containers import Container

from app.containers.hardened import HardenedContainerSpec, Rootfs, create_hardened
from app.controller.store import ControllerStore
from app.dependency_cache import _data_volume, _volume_context_tar
from app.labels import LABEL_SANDBOX_ID, LABEL_SERVICE
from app.previews._shared import _safe_relative_path, _slug
from app.previews.config import PreviewSettings
from app.previews.errors import PreviewOperationError
from app.previews.models import (
    PreviewConfiguration,
    PreviewNetworkAccess,
)
from app.previews.network import (
    PREVIEW_CONTAINER_PREFIX,
    _gateway_proxy,
    _network,
    _preview_egress,
)
from app.previews.progress import ProgressReporter, _ignore_progress
from app.previews.resources import (
    _ensure_preview_image,
    _preview_images,
    _remove_resources,
    _validate_built_image,
)
from app.previews.runtimes.environment import _secret_environment
from app.previews.sharing import (
    _connect_sandbox_database_endpoint,
    _managed_preview_database,
)


def _start_compose(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    files: dict[str, bytes],
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
    host_port: int,
    progress: ProgressReporter | None = None,
    secrets: dict[str, str] | None = None,
    controller_store: ControllerStore | None = None,
) -> dict[str, Any]:
    report = progress or _ignore_progress
    # Stored secrets reach the selected service only. Sidecars keep the
    # environment their Compose file declares and nothing more.
    application_environment = _secret_environment(config, secrets or {})
    managed_database = _managed_preview_database(
        docker_client,
        controller_store,
        labels[LABEL_SANDBOX_ID],
    )
    if managed_database is not None:
        application_environment.update(managed_database.environment)
    compose_path = _safe_relative_path(config.compose_file, field="compose_file")
    report("compose", f"Reading Compose file {compose_path}")
    content = files.get(compose_path)
    if content is None:
        raise PreviewOperationError(422, "Approved Compose file is missing")
    try:
        document = yaml.safe_load(content.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise PreviewOperationError(422, "Compose file is invalid") from error
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        raise PreviewOperationError(422, "Compose file has no services")
    if config.selected_service not in services:
        raise PreviewOperationError(422, "Selected preview service is not in Compose")

    report("network", f"Creating {config.network_access.value} preview network")
    network = _network(docker_client, run_id, labels, config.network_access)
    data_volumes: list[Any] = []
    named_volumes: dict[str, Any] = {}
    containers: list[Container] = []
    try:
        for service_name, raw_service in services.items():
            if not isinstance(raw_service, dict):
                raise PreviewOperationError(422, f"Compose service '{service_name}' is invalid")
            _validate_compose_service(str(service_name), raw_service)
            action = "Building" if raw_service.get("build") is not None else "Checking"
            report("compose-image", f"{action} image for service {service_name}")
            image = _compose_image(
                docker_client,
                settings,
                project_volume,
                str(service_name),
                raw_service,
                labels,
                run_id,
            )
            mounts = _compose_volumes(
                docker_client,
                project_volume,
                raw_service.get("volumes") or [],
                named_volumes,
                data_volumes,
                config,
                labels,
                run_id,
            )
            mounts.setdefault(
                project_volume,
                {"bind": "/sandbox", "mode": "ro"},
            )
            if managed_database is not None and service_name == config.selected_service:
                mounts.update(managed_database.volumes)
            service_labels = {**labels, LABEL_SERVICE: str(service_name)}
            ports = (
                {f"{config.container_port}/tcp": ("127.0.0.1", host_port)}
                if service_name == config.selected_service
                and config.network_access is PreviewNetworkAccess.INTERNET
                else None
            )
            report("compose-container", f"Creating service container {service_name}")
            container = create_hardened(docker_client, HardenedContainerSpec(
                image=image,
                command=_command(raw_service.get("command")),
                entrypoint=_command(raw_service.get("entrypoint")),
                name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-{_slug(str(service_name))}",
                rootfs=(
                    Rootfs.READ_ONLY
                    if bool(raw_service.get("read_only", False))
                    else Rootfs.WRITABLE
                ),
                working_dir=raw_service.get("working_dir"),
                user=raw_service.get("user"),
                environment=_compose_service_environment(
                    raw_service.get("environment"),
                    application_environment,
                    selected=service_name == config.selected_service,
                ),
                labels=service_labels,
                volumes=mounts,
                tmpfs_size="256m",
                network=network.name,
                egress=_preview_egress(config.network_access),
                ports=ports,
                restart_policy={"Name": "no"},
                mem_limit=settings.preview_memory,
                nano_cpus=1_000_000_000,
                pids_limit=256,
            ))
            network.disconnect(container)
            network.connect(container, aliases=[str(service_name)])
            if (
                managed_database is not None
                and managed_database.engine != "sqlite"
                and service_name == config.selected_service
            ):
                _connect_sandbox_database_endpoint(
                    docker_client,
                    managed_database,
                    container,
                )
            containers.append(container)

        by_service = {
            ((container.attrs.get("Config") or {}).get("Labels") or {}).get(
                LABEL_SERVICE, ""
            ): container
            for container in containers
        }
        for service_name in _service_order(services):
            report("compose-start", f"Starting service {service_name}")
            by_service[service_name].start()
    except Exception:
        _remove_resources(
            {
                "containers": containers,
                "networks": [network],
                "volumes": data_volumes,
                "images": [],
            },
            remove_data_volumes=True,
        )
        raise
    networks = [network]
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        report("gateway", "Creating the loopback preview gateway")
        gateway, gateway_network, gateway_volume = _gateway_proxy(
            docker_client,
            settings.inspection_image,
            network,
            config.selected_service,
            config.container_port,
            host_port,
            labels,
            run_id,
        )
        containers.append(gateway)
        networks.append(gateway_network)
        data_volumes.append(gateway_volume)
        report("gateway", "Loopback preview gateway started")
    return {
        "containers": containers,
        "networks": networks,
        "volumes": data_volumes,
        "images": _preview_images(docker_client, run_id),
        "borrowed_networks": (
            [docker_client.networks.get(managed_database.network_name)]
            if managed_database is not None and managed_database.engine != "sqlite"
            else []
        ),
    }


def _compose_service_environment(
    declared: Any,
    application_environment: dict[str, str],
    *,
    selected: bool,
) -> dict[str, str]:
    """Merges stored secrets into one Compose service.

    Only the selected service receives them. A sidecar keeps exactly the
    environment its Compose file declares.
    """
    environment = _compose_environment(declared)
    if selected:
        environment.update(application_environment)
    return environment


def _validate_compose_service(service_name: str, service: dict[str, Any]) -> None:
    forbidden = {
        "privileged",
        "network_mode",
        "pid",
        "ipc",
        "devices",
        "cap_add",
        # Compose cannot request Docker's no-new-privileges control.  Build
        # the key here so the boundary guard can reserve its direct spelling.
        "_".join(("security", "opt")),
        "env_file",
        "secrets",
        "configs",
    }
    present = sorted(key for key in forbidden if service.get(key))
    if present:
        raise PreviewOperationError(
            422,
            f"Compose service '{service_name}' uses blocked fields: {', '.join(present)}",
        )


def _compose_image(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    service_name: str,
    service: dict[str, Any],
    labels: dict[str, str],
    run_id: str,
) -> str:
    build = service.get("build")
    if build is not None:
        if isinstance(build, str):
            context = build
            dockerfile = "Dockerfile"
        elif isinstance(build, dict):
            context = str(build.get("context", "."))
            dockerfile = str(build.get("dockerfile", "Dockerfile"))
            if build.get("ssh") or build.get("secrets") or build.get("privileged"):
                raise PreviewOperationError(
                    422,
                    f"Compose service '{service_name}' requests blocked build privileges",
                )
        else:
            raise PreviewOperationError(422, f"Compose service '{service_name}' has invalid build")
        context_path = _safe_relative_path(context, field="build context", allow_dot=True)
        dockerfile_path = _safe_relative_path(dockerfile, field="dockerfile")
        archive = _volume_context_tar(
            docker_client,
            project_volume,
            context_path,
            settings.inspection_image,
        )
        tag = f"orchestrator-preview:{run_id}-{_slug(service_name)}"
        try:
            built_image, _ = docker_client.images.build(
                fileobj=io.BytesIO(archive),
                custom_context=True,
                dockerfile=dockerfile_path,
                tag=tag,
                rm=True,
                forcerm=True,
                labels=labels,
                timeout=settings.build_timeout_seconds,
            )
            _validate_built_image(built_image, settings)
        except (BuildError, APIError) as error:
            raise PreviewOperationError(
                422,
                f"Compose service '{service_name}' build failed: {error}",
            ) from error
        return tag
    image = service.get("image")
    if not isinstance(image, str) or not image:
        raise PreviewOperationError(
            422,
            f"Compose service '{service_name}' requires image or build",
        )
    if "${" in image:
        raise PreviewOperationError(422, "Compose environment interpolation is disabled")
    _ensure_preview_image(docker_client, image)
    return image


def _compose_volumes(
    docker_client: DockerClient,
    project_volume: str,
    declarations: Any,
    named_volumes: dict[str, Any],
    data_volumes: list[Any],
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
) -> dict[str, dict[str, str]]:
    if not isinstance(declarations, list):
        raise PreviewOperationError(422, "Compose service volumes must be a list")
    mounts: dict[str, dict[str, str]] = {}
    for declaration in declarations:
        source: str
        target: str
        mode = "rw"
        mount_type = "volume"
        if isinstance(declaration, str):
            pieces = declaration.split(":")
            if len(pieces) == 1:
                source = ""
                target = pieces[0]
            elif len(pieces) in {2, 3}:
                source, target = pieces[:2]
                if len(pieces) == 3 and pieces[2] == "ro":
                    mode = "ro"
            else:
                raise PreviewOperationError(422, "Compose volume syntax is unsupported")
            if source.startswith(".") or source.startswith("/"):
                mount_type = "bind"
        elif isinstance(declaration, dict):
            mount_type = str(declaration.get("type", "volume"))
            source = str(declaration.get("source", ""))
            target = str(declaration.get("target", ""))
            if declaration.get("read_only"):
                mode = "ro"
        else:
            raise PreviewOperationError(422, "Compose volume declaration is invalid")
        if not target.startswith("/"):
            raise PreviewOperationError(422, "Compose volume target must be absolute")
        if mount_type == "bind":
            if source not in {".", "./"}:
                raise PreviewOperationError(
                    422,
                    "Compose host bind mounts are blocked; only the sandbox root may be mounted",
                )
            mounts[project_volume] = {"bind": target, "mode": mode}
            continue
        logical_name = source or f"anonymous-{len(data_volumes) + 1}"
        volume = named_volumes.get(logical_name)
        if volume is None:
            persistent = logical_name in config.persistent_volumes
            volume = _data_volume(
                docker_client,
                run_id,
                logical_name,
                labels,
                persistent,
            )
            named_volumes[logical_name] = volume
            data_volumes.append(volume)
        mounts[volume.name] = {"bind": target, "mode": mode}
    return mounts


def _compose_environment(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        environment = {str(key): "" if item is None else str(item) for key, item in value.items()}
    elif isinstance(value, list):
        environment = {}
        for entry in value:
            key, separator, item = str(entry).partition("=")
            if not separator:
                raise PreviewOperationError(
                    422,
                    "Compose environment variables must include explicit values",
                )
            environment[key] = item
    else:
        raise PreviewOperationError(422, "Compose environment is invalid")
    if any("${" in item for item in environment.values()):
        raise PreviewOperationError(422, "Compose environment interpolation is disabled")
    return environment


def _service_order(services: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise PreviewOperationError(422, "Compose dependency cycle is unsupported")
        visiting.add(name)
        service = services.get(name) or {}
        dependencies = service.get("depends_on") or []
        dependency_names = dependencies if isinstance(dependencies, list) else dependencies.keys()
        for dependency in dependency_names:
            dependency_name = str(dependency)
            if dependency_name not in services:
                raise PreviewOperationError(
                    422,
                    f"Compose service '{name}' depends on missing service '{dependency_name}'",
                )
            visit(dependency_name)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for service_name in services:
        visit(str(service_name))
    return ordered


def _command(value: Any) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return [str(item) for item in value]
    raise PreviewOperationError(422, "Compose command or entrypoint is invalid")
