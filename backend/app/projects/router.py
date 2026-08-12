from pathlib import Path
from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.docker_client import get_docker_client
from app.controller.store import ControllerStore, get_controller_store
from app.previews.config import PreviewSettings, get_preview_settings
from app.previews.detection import capture_source_runtime_files, hashes
from app.projects.config import ProjectSettings, get_project_settings
from app.projects.models import (
    BrowseResponse,
    CopyProjectRequest,
    ProjectCopyJobsResponse,
    ProjectCopyJobStatus,
    RemoveProjectRequest,
    RemoveProjectResponse,
    ProjectRegistration,
    ProjectRegistrationsResponse,
)
from app.projects.service import (
    ProjectOperationError,
    browse_project_folders,
    inspect_project_copy_job,
    inspect_registered_project,
    list_project_copy_jobs,
    remove_project,
    list_registered_projects,
    register_project,
    project_id,
)

router = APIRouter(prefix="/projects", tags=["projects"])
ResponseType = TypeVar("ResponseType")


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    try:
        return function()
    except ProjectOperationError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.detail,
        ) from error
    except NotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Docker resource not found",
        ) from error
    except APIError as error:
        response_status = getattr(getattr(error, "response", None), "status_code", 0)
        if response_status == status.HTTP_409_CONFLICT:
            detail = "Docker rejected the action because the resource already exists"
            response_status = status.HTTP_409_CONFLICT
        else:
            detail = "Docker rejected the request"
            response_status = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=response_status, detail=detail) from error
    except DockerException as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Docker daemon is unavailable",
        ) from error


@router.post(
    "",
    response_model=ProjectCopyJobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_project_sandbox(
    request: CopyProjectRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[ProjectSettings, Depends(get_project_settings)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    preview_settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
) -> ProjectCopyJobStatus:
    job = _docker_response(
        lambda: register_project(docker_client, settings, request)
    )
    controller_store.register_sandbox(
        sandbox_id=job.sandbox_id,
        project_id=project_id(job.source_path),
        project_name=job.project_name,
        source_path=job.source_path,
        volume_name=job.volume_name,
        status=job.status,
        created_at=job.created_at,
    )
    try:
        baseline_files = capture_source_runtime_files(
            Path(job.source_path),
            maximum_file_bytes=preview_settings.maximum_file_bytes,
            maximum_snapshot_bytes=preview_settings.maximum_snapshot_bytes,
        )
    except (OSError, ValueError):
        controller_store.event(
            sandbox_id=job.sandbox_id,
            run_id=job.job_id,
            kind="sandbox.baseline_failed",
            payload={},
        )
    else:
        controller_store.record_initial_baseline(
            job.sandbox_id,
            baseline_files,
            hashes(baseline_files),
        )
    return job


@router.get("", response_model=ProjectRegistrationsResponse)
def get_project_registrations(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ProjectRegistrationsResponse:
    return _docker_response(lambda: list_registered_projects(docker_client))


@router.get("/browse", response_model=BrowseResponse)
def browse_projects_root(
    settings: Annotated[ProjectSettings, Depends(get_project_settings)],
    path: Annotated[str | None, Query()] = None,
) -> BrowseResponse:
    return _docker_response(lambda: browse_project_folders(settings, path))


@router.get("/copies", response_model=ProjectCopyJobsResponse)
def get_project_copy_jobs(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ProjectCopyJobsResponse:
    return _docker_response(lambda: list_project_copy_jobs(docker_client))


@router.get("/copies/{job_id}", response_model=ProjectCopyJobStatus)
def get_project_copy_job(
    job_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ProjectCopyJobStatus:
    return _docker_response(
        lambda: inspect_project_copy_job(docker_client, job_id)
    )


@router.delete("/{project_name}", response_model=RemoveProjectResponse)
def delete_project(
    project_name: str,
    request: RemoveProjectRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoveProjectResponse:
    if not request.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set confirm=true to remove the project and its Docker resources",
        )
    return _docker_response(
        lambda: remove_project(docker_client, controller_store, project_name)
    )


@router.get("/{project_name}", response_model=ProjectRegistration)
def get_project_registration(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> ProjectRegistration:
    return _docker_response(
        lambda: inspect_registered_project(
            docker_client,
            project_name,
            controller_store,
        )
    )
