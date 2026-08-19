from typing import Any

from docker.client import DockerClient

from app.platform.coercions import clamped_integer
from app.volumes.models import (
    DockerStorageStatusResponse,
    RunningVolume,
    RunningVolumesResponse,
    StorageUsage,
)


def list_running_volume_mounts(
    docker_client: DockerClient,
) -> RunningVolumesResponse:
    containers = docker_client.containers.list(filters={"status": "running"})
    volumes = [
        RunningVolume(
            type=mount.get("Type", "unknown"),
            name=mount.get("Name") or None,
            source=mount.get("Source", ""),
            destination=mount.get("Destination", ""),
            driver=mount.get("Driver", ""),
            mode=mount.get("Mode", ""),
            read_write=mount.get("RW", False),
            container_id=container.short_id,
            container_name=container.name,
        )
        for container in containers
        for mount in container.attrs.get("Mounts", [])
    ]
    volumes.sort(key=lambda volume: (volume.container_name, volume.destination))

    return RunningVolumesResponse(count=len(volumes), volumes=volumes)


def get_docker_storage_status(
    docker_client: DockerClient,
) -> DockerStorageStatusResponse:
    data = docker_client.api.df()

    images = data.get("Images") or []
    containers = data.get("Containers") or []
    volumes = data.get("Volumes") or []
    build_cache = data.get("BuildCache") or []

    image_usage = _storage_usage(
        summary=data.get("ImageUsage"),
        fallback_total_count=len(images),
        fallback_active_count=sum(
            clamped_integer(item.get("Containers")) > 0 for item in images
        ),
        fallback_size=clamped_integer(data.get("LayersSize")),
        fallback_reclaimable=sum(
            max(
                clamped_integer(item.get("Size"))
                - clamped_integer(item.get("SharedSize")),
                0,
            )
            for item in images
            if clamped_integer(item.get("Containers")) == 0
        ),
    )
    container_usage = _storage_usage(
        summary=data.get("ContainerUsage"),
        fallback_total_count=len(containers),
        fallback_active_count=sum(
            item.get("State") == "running" for item in containers
        ),
        fallback_size=sum(clamped_integer(item.get("SizeRw")) for item in containers),
        fallback_reclaimable=sum(
            clamped_integer(item.get("SizeRw"))
            for item in containers
            if item.get("State") != "running"
        ),
    )
    volume_usage = _storage_usage(
        summary=data.get("VolumeUsage"),
        fallback_total_count=len(volumes),
        fallback_active_count=sum(
            clamped_integer((item.get("UsageData") or {}).get("RefCount")) > 0
            for item in volumes
        ),
        fallback_size=sum(
            clamped_integer((item.get("UsageData") or {}).get("Size"))
            for item in volumes
        ),
        fallback_reclaimable=sum(
            clamped_integer((item.get("UsageData") or {}).get("Size"))
            for item in volumes
            if clamped_integer((item.get("UsageData") or {}).get("RefCount")) == 0
        ),
    )
    build_cache_usage = _storage_usage(
        summary=data.get("BuildCacheUsage"),
        fallback_total_count=len(build_cache),
        fallback_active_count=sum(bool(item.get("InUse")) for item in build_cache),
        fallback_size=sum(clamped_integer(item.get("Size")) for item in build_cache),
        fallback_reclaimable=sum(
            clamped_integer(item.get("Size"))
            for item in build_cache
            if not item.get("InUse")
        ),
    )

    categories = (image_usage, container_usage, volume_usage, build_cache_usage)
    total_size_bytes = sum(category.size_bytes for category in categories)
    total_reclaimable_bytes = sum(category.reclaimable_bytes for category in categories)

    return DockerStorageStatusResponse(
        total_size_bytes=total_size_bytes,
        total_size=_format_bytes(total_size_bytes),
        total_reclaimable_bytes=total_reclaimable_bytes,
        total_reclaimable=_format_bytes(total_reclaimable_bytes),
        images=image_usage,
        containers=container_usage,
        volumes=volume_usage,
        build_cache=build_cache_usage,
    )


def _storage_usage(
    *,
    summary: Any,
    fallback_total_count: int,
    fallback_active_count: int,
    fallback_size: int,
    fallback_reclaimable: int,
) -> StorageUsage:
    has_summary = isinstance(summary, dict) and "TotalSize" in summary
    total_count = (
        clamped_integer(summary.get("TotalCount"))
        if has_summary
        else fallback_total_count
    )
    active_count = (
        clamped_integer(summary.get("ActiveCount"))
        if has_summary
        else fallback_active_count
    )
    size_bytes = (
        clamped_integer(summary.get("TotalSize")) if has_summary else fallback_size
    )
    reclaimable_bytes = (
        clamped_integer(summary.get("Reclaimable"))
        if has_summary
        else fallback_reclaimable
    )

    return StorageUsage(
        total_count=total_count,
        active_count=active_count,
        size_bytes=size_bytes,
        size=_format_bytes(size_bytes),
        reclaimable_bytes=reclaimable_bytes,
        reclaimable=_format_bytes(reclaimable_bytes),
    )


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024

    raise AssertionError("unreachable")
