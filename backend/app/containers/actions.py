import base64
import io
import stat as stat_module
import tarfile
from pathlib import PurePosixPath
from typing import Any

from docker.client import DockerClient
from docker.errors import NotFound
from docker.models.containers import Container

from app.containers.models import (
    ContainerDetails,
    ContainerFileDetails,
    ContainerFileResponse,
    ContainerMount,
    ContainerNetwork,
    ContainerProcessesResponse,
    PruneContainersResponse,
    RemoveContainerResponse,
    StopContainerResponse,
)
from app.containers.service import container_summary

MAX_FILE_BYTES = 1_048_576


class ContainerOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def inspect_managed_container(
    docker_client: DockerClient,
    container_id: str,
) -> ContainerDetails:
    container = docker_client.containers.get(container_id)
    attrs = container.attrs
    state = attrs.get("State") or {}
    config = attrs.get("Config") or {}
    summary = container_summary(container)

    mounts = [
        ContainerMount(
            type=mount.get("Type", "unknown"),
            name=mount.get("Name") or None,
            source=mount.get("Source", ""),
            destination=mount.get("Destination", ""),
            driver=mount.get("Driver", ""),
            mode=mount.get("Mode", ""),
            read_write=mount.get("RW", False),
        )
        for mount in attrs.get("Mounts", [])
    ]
    mounts.sort(key=lambda mount: (mount.destination, mount.source))

    network_map = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    networks = [
        ContainerNetwork(
            name=name,
            network_id=network.get("NetworkID", ""),
            endpoint_id=network.get("EndpointID", ""),
            gateway=network.get("Gateway", ""),
            ip_address=network.get("IPAddress", ""),
            mac_address=network.get("MacAddress", ""),
        )
        for name, network in network_map.items()
    ]
    networks.sort(key=lambda network: network.name)

    return ContainerDetails(
        id=container.id,
        short_id=container.short_id,
        name=container.name,
        image=summary.image,
        image_id=attrs.get("Image", ""),
        status=container.status,
        created=summary.created,
        started_at=state.get("StartedAt", ""),
        finished_at=state.get("FinishedAt", ""),
        restart_count=_integer(attrs.get("RestartCount")),
        platform=attrs.get("Platform", ""),
        ports=summary.ports,
        mounts=mounts,
        networks=networks,
        labels=config.get("Labels") or {},
    )


def list_container_processes(
    docker_client: DockerClient,
    container_id: str,
) -> ContainerProcessesResponse:
    container = docker_client.containers.get(container_id)
    if container.status != "running":
        raise ContainerOperationError(
            409,
            "The container is not running, so it has no processes",
        )

    result = container.top()
    titles = [str(title) for title in result.get("Titles") or []]
    processes = [
        [_text(cell) for cell in row] for row in (result.get("Processes") or [])
    ]

    return ContainerProcessesResponse(
        container_id=container.short_id,
        container_name=container.name,
        titles=titles,
        count=len(processes),
        processes=processes,
    )


def remove_managed_container(
    docker_client: DockerClient,
    container_id: str,
    *,
    force: bool,
    remove_volumes: bool,
) -> RemoveContainerResponse:
    container = docker_client.containers.get(container_id)
    short_id = container.short_id
    name = container.name
    container.remove(force=force, v=remove_volumes)
    return RemoveContainerResponse(
        id=short_id,
        name=name,
        removed=True,
        removed_anonymous_volumes=remove_volumes,
    )


def prune_managed_containers(
    docker_client: DockerClient,
) -> PruneContainersResponse:
    result = docker_client.containers.prune()
    reclaimed_bytes = _integer(result.get("SpaceReclaimed"))
    return PruneContainersResponse(
        deleted=result.get("ContainersDeleted") or [],
        reclaimed_bytes=reclaimed_bytes,
        reclaimed=_format_bytes(reclaimed_bytes),
    )


def stop_managed_container(
    docker_client: DockerClient,
    container_id: str,
    *,
    timeout_seconds: int,
) -> StopContainerResponse:
    container = docker_client.containers.get(container_id)
    if container.status == "running":
        container.stop(timeout=timeout_seconds)

    return StopContainerResponse(
        id=container.short_id,
        name=container.name,
        status="stopped",
    )


def read_container_file(
    docker_client: DockerClient,
    container_id: str,
    file_path: str,
    *,
    max_bytes: int,
) -> ContainerFileResponse:
    container = docker_client.containers.get(container_id)
    normalized_path = _safe_container_path(file_path)

    try:
        chunks, file_stat = container.get_archive(normalized_path)
    except NotFound as error:
        raise ContainerOperationError(404, "File not found in the container") from error

    size_bytes = _integer(file_stat.get("size"))
    mode = _integer(file_stat.get("mode"))
    if size_bytes > max_bytes:
        _close_stream(chunks)
        raise ContainerOperationError(
            413,
            f"File exceeds the {max_bytes}-byte response limit",
        )

    content_bytes = _extract_file(chunks, max_bytes)
    try:
        content = content_bytes.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(content_bytes).decode("ascii")
        encoding = "base64"

    return ContainerFileResponse(
        container_id=container.short_id,
        container_name=container.name,
        file=ContainerFileDetails(
            path=normalized_path,
            name=file_stat.get("name", PurePosixPath(normalized_path).name),
            size_bytes=size_bytes,
            mode=oct(stat_module.S_IMODE(mode)),
            modified_at=str(file_stat.get("mtime", "")),
            link_target=file_stat.get("linkTarget", ""),
            encoding=encoding,
            content=content,
        ),
    )


def _safe_container_path(file_path: str) -> str:
    path = PurePosixPath(file_path)
    if not path.is_absolute() or ".." in path.parts or str(path) == "/":
        raise ContainerOperationError(
            400,
            "File path must be an absolute container file path",
        )
    return str(path)


def _extract_file(chunks: Any, max_bytes: int) -> bytes:
    archive_limit = max_bytes + 1_048_576
    archive_data = io.BytesIO()
    try:
        for chunk in chunks:
            if archive_data.tell() + len(chunk) > archive_limit:
                raise ContainerOperationError(
                    413,
                    "File archive exceeds the response limit",
                )
            archive_data.write(chunk)
    finally:
        _close_stream(chunks)

    archive_data.seek(0)
    try:
        with tarfile.open(fileobj=archive_data, mode="r:*") as archive:
            members = archive.getmembers()
            member = members[0] if members else None
            if member is None:
                raise ContainerOperationError(502, "Docker returned no file entry")
            if not member.isfile():
                raise ContainerOperationError(
                    400,
                    "The requested path is not a regular file",
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ContainerOperationError(502, "Docker returned no readable file")
            content = extracted.read(max_bytes + 1)
    except tarfile.TarError as error:
        raise ContainerOperationError(
            502,
            "Docker returned an invalid file archive",
        ) from error

    if len(content) > max_bytes:
        raise ContainerOperationError(413, "File exceeds the response limit")
    return content


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _integer(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024

    raise AssertionError("unreachable")
