from concurrent.futures import ThreadPoolExecutor
from typing import Any

from docker.client import DockerClient
from docker.models.containers import Container

from app.containers.models import (
    AllContainersResponse,
    ContainerPort,
    ContainerResourceStatus,
    ContainerStatusResponse,
    RunningContainer,
    RunningContainersResponse,
)
from app.platform.coercions import clamped_integer


def list_running_containers(
    docker_client: DockerClient,
) -> RunningContainersResponse:
    containers = docker_client.containers.list(filters={"status": "running"})
    response = [container_summary(container) for container in containers]
    response.sort(key=lambda container: container.name)

    return RunningContainersResponse(count=len(response), containers=response)


def list_all_containers(
    docker_client: DockerClient,
) -> AllContainersResponse:
    containers = docker_client.containers.list(all=True)
    response = [container_summary(container) for container in containers]
    response.sort(key=lambda container: container.name)
    return AllContainersResponse(count=len(response), containers=response)


def container_summary(container: Container) -> RunningContainer:
    return RunningContainer(
        id=container.short_id,
        name=container.name,
        image=(container.attrs.get("Config") or {}).get("Image", ""),
        status=container.status,
        created=container.attrs.get("Created", ""),
        ports=_container_ports(container),
    )


def get_running_container_status(
    docker_client: DockerClient,
) -> ContainerStatusResponse:
    containers = docker_client.containers.list(filters={"status": "running"})

    if containers:
        worker_count = min(len(containers), 8)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            statuses = list(executor.map(_container_status, containers))
    else:
        statuses = []

    statuses.sort(key=lambda container: container.name)

    total_memory_usage_bytes = sum(
        container.memory_usage_bytes for container in statuses
    )

    return ContainerStatusResponse(
        count=len(statuses),
        total_cpu_percent=round(
            sum(container.cpu_percent for container in statuses), 2
        ),
        total_memory_usage_bytes=total_memory_usage_bytes,
        total_memory_usage=_format_bytes(total_memory_usage_bytes),
        total_network_received_bytes=sum(
            container.network_received_bytes for container in statuses
        ),
        total_network_sent_bytes=sum(
            container.network_sent_bytes for container in statuses
        ),
        total_block_read_bytes=sum(
            container.block_read_bytes for container in statuses
        ),
        total_block_write_bytes=sum(
            container.block_write_bytes for container in statuses
        ),
        total_pids=sum(container.pids for container in statuses),
        containers=statuses,
    )


def _container_ports(container: Container) -> list[ContainerPort]:
    port_map = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    ports: list[ContainerPort] = []

    for container_address, bindings in port_map.items():
        port_text, protocol = container_address.split("/", maxsplit=1)
        if not bindings:
            ports.append(
                ContainerPort(
                    container_port=int(port_text),
                    protocol=protocol,
                    host_ip=None,
                    host_port=None,
                )
            )
            continue

        ports.extend(
            ContainerPort(
                container_port=int(port_text),
                protocol=protocol,
                host_ip=binding.get("HostIp") or None,
                host_port=_optional_integer(binding.get("HostPort")),
            )
            for binding in bindings
        )

    ports.sort(
        key=lambda port: (
            port.container_port,
            port.protocol,
            port.host_ip or "",
            port.host_port or 0,
        )
    )
    return ports


def _container_status(container: Container) -> ContainerResourceStatus:
    stats = container.stats(stream=False)
    memory_stats = stats.get("memory_stats") or {}
    memory_details = memory_stats.get("stats") or {}
    memory_cache = clamped_integer(
        memory_details.get(
            "inactive_file",
            memory_details.get(
                "total_inactive_file",
                memory_details.get("cache", 0),
            ),
        )
    )
    memory_usage_bytes = max(
        clamped_integer(memory_stats.get("usage")) - memory_cache,
        0,
    )
    memory_limit_bytes = clamped_integer(memory_stats.get("limit"))
    memory_percent = (
        round(memory_usage_bytes / memory_limit_bytes * 100, 2)
        if memory_limit_bytes
        else 0.0
    )

    network_stats = (stats.get("networks") or {}).values()
    block_stats = (stats.get("blkio_stats") or {}).get(
        "io_service_bytes_recursive"
    ) or []

    return ContainerResourceStatus(
        id=container.short_id,
        name=container.name,
        cpu_percent=_cpu_percent(stats),
        memory_usage_bytes=memory_usage_bytes,
        memory_usage=_format_bytes(memory_usage_bytes),
        memory_limit_bytes=memory_limit_bytes,
        memory_limit=_format_bytes(memory_limit_bytes),
        memory_percent=memory_percent,
        network_received_bytes=sum(
            clamped_integer(network.get("rx_bytes")) for network in network_stats
        ),
        network_sent_bytes=sum(
            clamped_integer(network.get("tx_bytes"))
            for network in (stats.get("networks") or {}).values()
        ),
        block_read_bytes=sum(
            clamped_integer(operation.get("value"))
            for operation in block_stats
            if str(operation.get("op", "")).lower() == "read"
        ),
        block_write_bytes=sum(
            clamped_integer(operation.get("value"))
            for operation in block_stats
            if str(operation.get("op", "")).lower() == "write"
        ),
        pids=clamped_integer((stats.get("pids_stats") or {}).get("current")),
        sampled_at=stats.get("read", ""),
    )


def _cpu_percent(stats: dict[str, Any]) -> float:
    cpu_stats = stats.get("cpu_stats") or {}
    previous_stats = stats.get("precpu_stats") or {}
    cpu_usage = cpu_stats.get("cpu_usage") or {}
    previous_usage = previous_stats.get("cpu_usage") or {}

    cpu_delta = clamped_integer(cpu_usage.get("total_usage")) - clamped_integer(
        previous_usage.get("total_usage")
    )
    system_delta = clamped_integer(cpu_stats.get("system_cpu_usage")) - clamped_integer(
        previous_stats.get("system_cpu_usage")
    )
    online_cpus = clamped_integer(cpu_stats.get("online_cpus"))
    if not online_cpus:
        online_cpus = len(cpu_usage.get("percpu_usage") or []) or 1

    if cpu_delta <= 0 or system_delta <= 0:
        return 0.0

    return round(cpu_delta / system_delta * online_cpus * 100, 2)


def _optional_integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return clamped_integer(value)


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024

    raise AssertionError("unreachable")
