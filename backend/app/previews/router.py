from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, DockerException, NotFound
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.previews.config import PreviewSettings, get_preview_settings
from app.previews.models import (
    ImportProjectSecretsResponse,
    KeepAliveRequest,
    PreviewAction,
    PreviewActionRequest,
    PreviewLogs,
    PreviewProposal,
    PreviewRun,
    ProjectDatabaseSharing,
    ProjectSecrets,
    SetProjectSecretsRequest,
    StartPreviewRequest,
    StopPreviewRequest,
    StopPreviewResponse,
)
from app.previews.service import (
    PreviewOperationError,
    database_sharing_state,
    delete_project_secret,
    get_current_preview,
    get_project_secrets,
    import_project_secrets,
    preview_creation_logs,
    preview_logs,
    propose_preview,
    restart_preview,
    reuse_preview,
    set_project_secrets,
    start_preview,
    stop_preview,
)


router = APIRouter(prefix="/projects/{project_name}", tags=["previews"])
ResponseType = TypeVar("ResponseType")


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    try:
        return function()
    except PreviewOperationError as error:
        raise HTTPException(error.status_code, error.detail) from error
    except NotFound as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Docker resource not found",
        ) from error
    except APIError as error:
        response_status = getattr(getattr(error, "response", None), "status_code", 0)
        raise HTTPException(
            response_status or status.HTTP_502_BAD_GATEWAY,
            "Docker rejected the preview operation",
        ) from error
    except DockerException as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Docker daemon is unavailable",
        ) from error


@router.post("/preview-proposals", response_model=PreviewProposal)
def inspect_preview(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
) -> PreviewProposal:
    return _docker_response(
        lambda: propose_preview(
            docker_client,
            controller_store,
            settings,
            project_name,
        )
    )


@router.post(
    "/previews",
    response_model=PreviewRun,
    status_code=status.HTTP_201_CREATED,
)
def create_preview(
    project_name: str,
    request: StartPreviewRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
) -> PreviewRun:
    return _docker_response(
        lambda: start_preview(
            docker_client,
            controller_store,
            settings,
            project_name,
            request,
        )
    )


@router.get("/database-sharing", response_model=ProjectDatabaseSharing)
def get_database_sharing(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> ProjectDatabaseSharing:
    return _docker_response(
        lambda: database_sharing_state(
            docker_client,
            controller_store,
            project_name,
        )
    )


@router.get("/preview-proposals/{proposal_id}/logs", response_model=PreviewLogs)
def get_preview_creation_logs(
    project_name: str,
    proposal_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PreviewLogs:
    return _docker_response(
        lambda: preview_creation_logs(
            docker_client,
            controller_store,
            project_name,
            proposal_id,
        )
    )


@router.get("/previews/current", response_model=PreviewRun)
def get_preview(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PreviewRun:
    return _docker_response(
        lambda: get_current_preview(
            docker_client,
            controller_store,
            project_name,
            touch=True,
            expiry_minutes=None,
        )
    )


@router.post("/previews/current/actions", response_model=PreviewRun)
def act_on_preview(
    project_name: str,
    request: PreviewActionRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
) -> PreviewRun:
    if not request.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set confirm=true to change the active preview",
        )
    if request.action is PreviewAction.REUSE:
        return _docker_response(
            lambda: reuse_preview(
                docker_client,
                controller_store,
                settings,
                project_name,
            )
        )
    if request.action is PreviewAction.RESTART:
        return _docker_response(
            lambda: restart_preview(
                docker_client,
                controller_store,
                settings,
                project_name,
            )
        )
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "Rebuild requires a new inspected and approved proposal",
    )


@router.post("/previews/current/keep-alive", response_model=PreviewRun)
def keep_preview_alive(
    project_name: str,
    request: KeepAliveRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PreviewRun:
    return _docker_response(
        lambda: get_current_preview(
            docker_client,
            controller_store,
            project_name,
            touch=True,
            expiry_minutes=request.expiry_minutes,
        )
    )


@router.get("/previews/current/logs", response_model=PreviewLogs)
def get_preview_logs(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
) -> PreviewLogs:
    return _docker_response(
        lambda: preview_logs(
            docker_client,
            controller_store,
            settings,
            project_name,
        )
    )


@router.get("/secrets", response_model=ProjectSecrets)
def get_secrets(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> ProjectSecrets:
    return _docker_response(
        lambda: get_project_secrets(
            docker_client,
            controller_store,
            project_name,
        )
    )


@router.put("/secrets", response_model=ProjectSecrets)
def put_secrets(
    project_name: str,
    request: SetProjectSecretsRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> ProjectSecrets:
    return _docker_response(
        lambda: set_project_secrets(
            docker_client,
            controller_store,
            project_name,
            request,
        )
    )


@router.delete("/secrets/{name}", response_model=ProjectSecrets)
def delete_secret(
    project_name: str,
    name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> ProjectSecrets:
    return _docker_response(
        lambda: delete_project_secret(
            docker_client,
            controller_store,
            project_name,
            name,
        )
    )


@router.post("/secrets/import", response_model=ImportProjectSecretsResponse)
def post_import_secrets(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
) -> ImportProjectSecretsResponse:
    return _docker_response(
        lambda: import_project_secrets(
            docker_client,
            controller_store,
            settings,
            project_name,
        )
    )


@router.delete("/previews/current", response_model=StopPreviewResponse)
def delete_preview(
    project_name: str,
    request: Annotated[StopPreviewRequest, Body()],
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> StopPreviewResponse:
    if not request.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set confirm=true to stop this preview",
        )
    return _docker_response(
        lambda: stop_preview(
            docker_client,
            controller_store,
            project_name,
            remove_data_volumes=request.remove_data_volumes,
        )
    )
