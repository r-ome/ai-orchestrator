from pathlib import Path
from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

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
    RegisterRemoteProjectRequest,
    RemoteProject,
    RemoteProjectsResponse,
    RemoveProjectRequest,
    RemoveProjectResponse,
    RemoveRemoteProjectResponse,
    ProjectRegistration,
    ProjectRegistrationsResponse,
)
from app.projects.remote import normalize_remote_url, project_id_for_remote
from app.sandboxes.naming import mirror_volume, validate_mirror_ownership
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


def _remote_project(row: dict[str, object]) -> RemoteProject:
    return RemoteProject(
        project_id=str(row["id"]),
        remote_url=str(row["remote_url"]),
        # An empty default branch means the mirror has never been fetched.
        # Report that as absent rather than as an empty branch name.
        default_branch=str(row.get("default_branch") or "") or None,
        mirror_volume=str(row.get("mirror_volume") or "") or None,
        mirror_fetched_at=str(row.get("mirror_fetched_at") or "") or None,
        sandbox_count=int(row.get("sandbox_count") or 0),
        created_at=str(row.get("created_at") or ""),
    )


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


# These routes must stay above the `/{project_name}` routes below. FastAPI
# matches in declaration order, so a static segment only wins if it is declared
# first, exactly as `/browse` and `/copies` already rely on. A legacy project
# literally named "remote" would be shadowed here; legacy names come from
# folder names, and none is reserved.
@router.get("/remote", response_model=RemoteProjectsResponse)
def list_remote_projects(
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoteProjectsResponse:
    projects = [_remote_project(row) for row in controller_store.v1_projects()]
    return RemoteProjectsResponse(count=len(projects), projects=projects)


@router.post(
    "/remote",
    response_model=RemoteProject,
    status_code=status.HTTP_201_CREATED,
)
def register_remote_project(
    request: RegisterRemoteProjectRequest,
    response: Response,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoteProject:
    """Register a project without creating a sandbox in it.

    The mirror is not fetched here. Creating the first sandbox already fetches
    it under the project mirror lock, and duplicating that here would add a
    second, unlocked path to the same shared volume.
    """
    try:
        remote_url = normalize_remote_url(request.remote_url)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    identifier = project_id_for_remote(remote_url)
    existed = controller_store.v1_project(identifier) is not None
    controller_store.register_v1_project(
        project_id=identifier,
        remote_url=remote_url,
        default_branch="",
        mirror_volume=mirror_volume(identifier),
        created_at="",
    )
    row = controller_store.v1_project(identifier)
    if row is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "project registration did not persist",
        )
    if existed:
        response.status_code = status.HTTP_200_OK
    return _remote_project(row)


@router.get("/remote/{project_id}", response_model=RemoteProject)
def get_remote_project(
    project_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoteProject:
    row = controller_store.v1_project(project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no project {project_id!r}")
    return _remote_project(row)


@router.delete(
    "/remote/{project_id}",
    response_model=RemoveRemoteProjectResponse,
)
def delete_remote_project(
    project_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoveRemoteProjectResponse:
    """Remove a project that has no sandboxes left, and its shared mirror.

    Sandbox teardown stays exclusively at `DELETE /sandboxes/{id}`, the only
    path that takes the lifecycle lease and drains writers. This refuses while
    any sandbox remains rather than cascading into one.
    """
    row = controller_store.v1_project(project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no project {project_id!r}")

    volume_name = str(row.get("mirror_volume") or mirror_volume(project_id))
    try:
        controller_store.delete_v1_project(project_id)
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error

    # Re-validate ownership before removing. The volume name is derived, so a
    # stale or hand-made volume could otherwise be removed on a project's word.
    removed: str | None = None
    try:
        volume = docker_client.volumes.get(volume_name)
        validate_mirror_ownership(volume, project_id=project_id)
    except NotFound:
        pass
    except ValueError:
        pass
    else:
        volume.remove(force=True)
        removed = volume_name
    return RemoveRemoteProjectResponse(
        project_id=project_id,
        removed_mirror_volume=removed,
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
