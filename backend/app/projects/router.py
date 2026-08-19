from collections.abc import Callable
from typing import Annotated, TypeVar

from docker.client import DockerClient
from docker.errors import NotFound
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.controller.store import ControllerStore, get_controller_store
from app.platform.docker_client import get_docker_client
from app.platform.docker_errors import (
    DockerErrorPolicy,
    PassThroughApiError,
    docker_response,
)
from app.platform.naming import mirror_volume, validate_mirror_ownership
from app.platform.remote import normalize_remote_url, project_id_for_remote
from app.projects.models import (
    RegisterRemoteProjectRequest,
    RemoteProject,
    RemoteProjectsResponse,
    RemoveRemoteProjectResponse,
)

router = APIRouter(prefix="/projects", tags=["projects"])
ResponseType = TypeVar("ResponseType")


class ProjectOperationError(Exception):
    def __init__(self, status_code: int, detail: object) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


_DOCKER_ERRORS = DockerErrorPolicy(
    domain_errors=(ProjectOperationError,),
    api_error=PassThroughApiError("Docker rejected the project operation"),
)


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    return docker_response(function, _DOCKER_ERRORS)


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


@router.get("/remote", response_model=RemoteProjectsResponse)
def list_remote_projects(
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoteProjectsResponse:
    projects = _docker_response(
        lambda: [_remote_project(row) for row in controller_store.v1_projects()]
    )
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
    row, existed = _docker_response(
        lambda: _register_remote_project(controller_store, remote_url)
    )
    if existed:
        response.status_code = status.HTTP_200_OK
    return _remote_project(row)


@router.get("/remote/{project_id}", response_model=RemoteProject)
def get_remote_project(
    project_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoteProject:
    row = _docker_response(
        lambda: _require_remote_project(controller_store, project_id)
    )
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
    removed = _docker_response(
        lambda: _delete_remote_project(docker_client, controller_store, project_id)
    )
    return RemoveRemoteProjectResponse(
        project_id=project_id,
        removed_mirror_volume=removed,
    )


def _register_remote_project(
    controller_store: ControllerStore,
    remote_url: str,
) -> tuple[dict[str, object], bool]:
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
        raise ProjectOperationError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "project registration did not persist",
        )
    return row, existed


def _require_remote_project(
    controller_store: ControllerStore,
    project_id: str,
) -> dict[str, object]:
    row = controller_store.v1_project(project_id)
    if row is None:
        raise ProjectOperationError(
            status.HTTP_404_NOT_FOUND, f"no project {project_id!r}"
        )
    return row


def _delete_remote_project(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_id: str,
) -> str | None:
    row = _require_remote_project(controller_store, project_id)
    volume_name = str(row.get("mirror_volume") or mirror_volume(project_id))
    try:
        controller_store.delete_v1_project(project_id)
    except ValueError as error:
        raise ProjectOperationError(status.HTTP_409_CONFLICT, str(error)) from error

    # Re-validate ownership before removing. The volume name is derived, so a
    # stale or hand-made volume could otherwise be removed on a project's word.
    try:
        volume = docker_client.volumes.get(volume_name)
        validate_mirror_ownership(volume, project_id=project_id)
    except NotFound:
        return None
    except ValueError:
        return None
    volume.remove(force=True)
    return volume_name
