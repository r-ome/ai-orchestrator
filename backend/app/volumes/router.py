from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.platform.docker_client import get_docker_client
from app.platform.docker_errors import ConflictApiError, DockerErrorPolicy, docker_response
from app.volumes.actions import (
    MAX_FILE_BYTES,
    VolumeOperationError,
    inspect_managed_volume,
    list_managed_volumes,
    prune_managed_volumes,
    read_volume_file,
    remove_managed_volume,
    stop_attached_container,
)
from app.volumes.models import (
    ConfirmAction,
    DockerStorageStatusResponse,
    ManagedVolume,
    ManagedVolumesResponse,
    PruneVolumesResponse,
    RemoveVolumeResponse,
    RunningVolumesResponse,
    StopAttachedContainerResponse,
    StopContainerAction,
    VolumeFileResponse,
)
from app.volumes.service import (
    get_docker_storage_status,
    list_running_volume_mounts,
)

router = APIRouter(prefix="/volumes", tags=["volumes"])
ResponseType = TypeVar("ResponseType")


_DOCKER_ERRORS = DockerErrorPolicy(
    domain_errors=(VolumeOperationError,),
    api_error=ConflictApiError(
        "Docker rejected the action because the resource is in use"
    ),
)


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    return docker_response(function, _DOCKER_ERRORS)


@router.get("", response_model=RunningVolumesResponse)
def get_running_volumes(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> RunningVolumesResponse:
    return _docker_response(lambda: list_running_volume_mounts(docker_client))


@router.get("/status", response_model=DockerStorageStatusResponse)
def get_storage_status(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> DockerStorageStatusResponse:
    return _docker_response(lambda: get_docker_storage_status(docker_client))


@router.get("/all", response_model=ManagedVolumesResponse)
def get_all_managed_volumes(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ManagedVolumesResponse:
    return _docker_response(lambda: list_managed_volumes(docker_client))


@router.post("/prune", response_model=PruneVolumesResponse)
def prune_volumes(
    request: ConfirmAction,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> PruneVolumesResponse:
    _require_confirmation(request.confirm)
    return _docker_response(lambda: prune_managed_volumes(docker_client))


@router.get("/{volume_name}", response_model=ManagedVolume)
def inspect_volume(
    volume_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ManagedVolume:
    return _docker_response(
        lambda: inspect_managed_volume(docker_client, volume_name)
    )


@router.delete("/{volume_name}", response_model=RemoveVolumeResponse)
def remove_volume(
    volume_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    confirm: Annotated[bool, Query(description="Confirm permanent deletion")] = False,
    force: Annotated[bool, Query(description="Pass force to the volume driver")] = False,
) -> RemoveVolumeResponse:
    _require_confirmation(confirm)
    return _docker_response(
        lambda: remove_managed_volume(
            docker_client,
            volume_name,
            force=force,
        )
    )


@router.post(
    "/{volume_name}/containers/{container_id}/stop",
    response_model=StopAttachedContainerResponse,
)
def stop_volume_container(
    volume_name: str,
    container_id: str,
    request: StopContainerAction,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> StopAttachedContainerResponse:
    _require_confirmation(request.confirm)
    if not 1 <= request.timeout_seconds <= 60:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="timeout_seconds must be between 1 and 60",
        )
    return _docker_response(
        lambda: stop_attached_container(
            docker_client,
            volume_name,
            container_id,
            timeout_seconds=request.timeout_seconds,
        )
    )


@router.get("/{volume_name}/files", response_model=VolumeFileResponse)
def get_volume_file(
    volume_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    path: Annotated[str, Query(min_length=1)],
    container_id: Annotated[str | None, Query()] = None,
    max_bytes: Annotated[int, Query(ge=1, le=MAX_FILE_BYTES)] = 65_536,
) -> VolumeFileResponse:
    return _docker_response(
        lambda: read_volume_file(
            docker_client,
            volume_name,
            path,
            container_id=container_id,
            max_bytes=max_bytes,
        )
    )


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set confirm=true to run this destructive action",
        )
