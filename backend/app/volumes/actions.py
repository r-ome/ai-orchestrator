import base64
import io
import posixpath
import stat as stat_module
import tarfile
from pathlib import PurePosixPath
from typing import Any

from docker.client import DockerClient
from docker.errors import NotFound
from docker.models.containers import Container
from docker.models.volumes import Volume

from app.volumes.models import (
    ManagedVolume,
    ManagedVolumesResponse,
    PruneVolumesResponse,
    RemoveVolumeResponse,
    StopAttachedContainerResponse,
    VolumeAttachment,
    VolumeFileDetails,
    VolumeFileResponse,
)

MAX_FILE_BYTES = 1_048_576


class VolumeOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def list_managed_volumes(docker_client: DockerClient) -> ManagedVolumesResponse:
    volumes = docker_client.volumes.list()
    containers = docker_client.containers.list(all=True)
    response = [
        _managed_volume(volume, containers)
        for volume in volumes
    ]
    response.sort(key=lambda volume: volume.name)
    return ManagedVolumesResponse(count=len(response), volumes=response)


def inspect_managed_volume(
    docker_client: DockerClient,
    volume_name: str,
) -> ManagedVolume:
    volume = docker_client.volumes.get(volume_name)
    containers = docker_client.containers.list(all=True)
    return _managed_volume(volume, containers)


def remove_managed_volume(
    docker_client: DockerClient,
    volume_name: str,
    *,
    force: bool,
) -> RemoveVolumeResponse:
    volume = docker_client.volumes.get(volume_name)
    volume.remove(force=force)
    return RemoveVolumeResponse(name=volume_name, removed=True)


def prune_managed_volumes(docker_client: DockerClient) -> PruneVolumesResponse:
    result = docker_client.volumes.prune()
    reclaimed_bytes = _integer(result.get("SpaceReclaimed"))
    return PruneVolumesResponse(
        deleted=result.get("VolumesDeleted") or [],
        reclaimed_bytes=reclaimed_bytes,
        reclaimed=_format_bytes(reclaimed_bytes),
    )


def stop_attached_container(
    docker_client: DockerClient,
    volume_name: str,
    container_id: str,
    *,
    timeout_seconds: int,
) -> StopAttachedContainerResponse:
    docker_client.volumes.get(volume_name)
    container = docker_client.containers.get(container_id)
    if not _volume_mounts(container, volume_name):
        raise VolumeOperationError(
            409,
            f"Container '{container_id}' does not use volume '{volume_name}'",
        )

    if container.status == "running":
        container.stop(timeout=timeout_seconds)

    return StopAttachedContainerResponse(
        volume_name=volume_name,
        container_id=container.short_id,
        container_name=container.name,
        status="stopped",
    )


def read_volume_file(
    docker_client: DockerClient,
    volume_name: str,
    file_path: str,
    *,
    container_id: str | None,
    max_bytes: int,
) -> VolumeFileResponse:
    docker_client.volumes.get(volume_name)
    relative_path = _safe_relative_path(file_path)
    container, destination = _reader_container(
        docker_client,
        volume_name,
        container_id,
    )
    container_path = posixpath.join(destination, relative_path)

    try:
        chunks, file_stat = container.get_archive(container_path)
    except NotFound as error:
        raise VolumeOperationError(404, "File not found in the volume") from error

    size_bytes = _integer(file_stat.get("size"))
    mode = _integer(file_stat.get("mode"))
    if size_bytes > max_bytes:
        _close_stream(chunks)
        raise VolumeOperationError(
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

    return VolumeFileResponse(
        volume_name=volume_name,
        container_id=container.short_id,
        container_name=container.name,
        container_path=container_path,
        file=VolumeFileDetails(
            path=relative_path,
            name=file_stat.get("name", PurePosixPath(relative_path).name),
            size_bytes=size_bytes,
            mode=oct(stat_module.S_IMODE(mode)),
            modified_at=str(file_stat.get("mtime", "")),
            link_target=file_stat.get("linkTarget", ""),
            encoding=encoding,
            content=content,
        ),
    )


def _managed_volume(
    volume: Volume,
    containers: list[Container],
) -> ManagedVolume:
    attrs = volume.attrs
    attachments = [
        VolumeAttachment(
            container_id=container.short_id,
            container_name=container.name,
            container_status=container.status,
            destination=mount.get("Destination", ""),
            read_write=mount.get("RW", False),
        )
        for container in containers
        for mount in _volume_mounts(container, volume.name)
    ]
    attachments.sort(
        key=lambda attachment: (
            attachment.container_name,
            attachment.destination,
        )
    )

    return ManagedVolume(
        name=volume.name,
        driver=attrs.get("Driver", ""),
        mountpoint=attrs.get("Mountpoint", ""),
        created_at=attrs.get("CreatedAt", ""),
        scope=attrs.get("Scope", ""),
        labels=attrs.get("Labels"),
        options=attrs.get("Options"),
        attachments=attachments,
    )


def _volume_mounts(container: Container, volume_name: str) -> list[dict[str, Any]]:
    return [
        mount
        for mount in container.attrs.get("Mounts", [])
        if mount.get("Type") == "volume" and mount.get("Name") == volume_name
    ]


def _reader_container(
    docker_client: DockerClient,
    volume_name: str,
    container_id: str | None,
) -> tuple[Container, str]:
    containers = docker_client.containers.list(
        filters={"status": "running"},
    )
    for container in containers:
        if container_id and container_id not in {
            container.id,
            container.short_id,
            container.name,
        }:
            continue
        mounts = _volume_mounts(container, volume_name)
        if mounts:
            return container, mounts[0].get("Destination", "")

    if container_id:
        detail = (
            f"Running container '{container_id}' does not use volume "
            f"'{volume_name}'"
        )
    else:
        detail = f"Volume '{volume_name}' has no running container for file access"
    raise VolumeOperationError(409, detail)


def _safe_relative_path(file_path: str) -> str:
    path = PurePosixPath(file_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise VolumeOperationError(400, "File path must be relative to the volume")
    normalized = str(path)
    if normalized in {"", "."}:
        raise VolumeOperationError(400, "File path must identify a file")
    return normalized


def _extract_file(chunks: Any, max_bytes: int) -> bytes:
    archive_limit = max_bytes + 1_048_576
    archive_data = io.BytesIO()
    try:
        for chunk in chunks:
            if archive_data.tell() + len(chunk) > archive_limit:
                raise VolumeOperationError(413, "File archive exceeds the response limit")
            archive_data.write(chunk)
    finally:
        _close_stream(chunks)

    archive_data.seek(0)
    try:
        with tarfile.open(fileobj=archive_data, mode="r:*") as archive:
            members = archive.getmembers()
            member = members[0] if members else None
            if member is None:
                raise VolumeOperationError(502, "Docker returned no file entry")
            if not member.isfile():
                raise VolumeOperationError(
                    400,
                    "The requested path is not a regular file",
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise VolumeOperationError(502, "Docker returned no readable file")
            content = extracted.read(max_bytes + 1)
    except tarfile.TarError as error:
        raise VolumeOperationError(502, "Docker returned an invalid file archive") from error

    if len(content) > max_bytes:
        raise VolumeOperationError(413, "File exceeds the response limit")
    return content


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


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
