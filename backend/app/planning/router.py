from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, ContainerError, DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, status

from app.controller.store import ControllerStore, get_controller_store
from app.delegation.config import get_routing_settings
from app.docker_client import get_docker_client
from app.planning.config import (
    PlanningSettings,
    get_planning_settings,
    reasoning_effort_choices,
)
from app.planning.models import (
    CreatePlanningSessionRequest,
    PlanningDefaults,
    PlanningMessageRaw,
    PlanningMessageRequest,
    PlanningSession,
    PlanningSessionDetail,
    PlanningSessionsResponse,
)
from app.planning.runner import PlanningTurnError
from app.planning.service import (
    PlanningOperationError,
    cancel_session,
    confirm_understanding,
    correct_understanding,
    create_session,
    get_message_raw_output,
    get_session,
    list_sessions,
    post_message,
    proceed_without_confirmation,
)


router = APIRouter(prefix="/projects/{project_name}/planning", tags=["planning"])
ResponseType = TypeVar("ResponseType")


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    try:
        return function()
    except PlanningOperationError as error:
        raise HTTPException(error.status_code, error.detail) from error
    except PlanningTurnError as error:
        raise HTTPException(error.status_code, error.detail) from error
    except NotFound as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Docker resource not found",
        ) from error
    except ContainerError as error:
        # The daemon is reachable: a git helper container ran and exited non-zero.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Task git container failed: {error}",
        ) from error
    except APIError as error:
        response_status = getattr(getattr(error, "response", None), "status_code", 0)
        raise HTTPException(
            response_status or status.HTTP_502_BAD_GATEWAY,
            "Docker rejected the task operation",
        ) from error
    except DockerException as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Docker daemon is unavailable",
        ) from error


@router.get("/defaults", response_model=PlanningDefaults)
async def defaults(
    project_name: str,
    settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
) -> PlanningDefaults:
    del project_name
    return PlanningDefaults(
        clarifier_provider=settings.clarifier_provider,
        planner_provider=settings.planner_provider,
        reviewer_provider=settings.reviewer_provider,
        claude_model=settings.claude_model,
        codex_model=settings.codex_model,
        codex_reasoning_effort=settings.codex_reasoning_effort,
        max_review_turns=settings.max_review_turns,
        models_by_provider={
            provider: list(models)
            for provider, models in get_routing_settings().catalogue().items()
        },
        reasoning_efforts=reasoning_effort_choices(settings),
    )


@router.post("/sessions", response_model=PlanningSession, status_code=status.HTTP_201_CREATED)
async def create(
    project_name: str,
    request: CreatePlanningSessionRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
) -> PlanningSession:
    return _docker_response(
        lambda: create_session(docker_client, controller_store, settings, project_name, request)
    )


@router.get("/sessions", response_model=PlanningSessionsResponse)
async def list_for_project(
    project_name: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PlanningSessionsResponse:
    return _docker_response(lambda: list_sessions(docker_client, controller_store, project_name))


@router.get("/sessions/{session_id}", response_model=PlanningSessionDetail)
async def get_one(
    project_name: str,
    session_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PlanningSessionDetail:
    return _docker_response(lambda: get_session(controller_store, project_name, session_id))


@router.get("/sessions/{session_id}/messages/{sequence}/raw", response_model=PlanningMessageRaw)
async def message_raw(
    project_name: str,
    session_id: str,
    sequence: int,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PlanningMessageRaw:
    return _docker_response(
        lambda: get_message_raw_output(controller_store, project_name, session_id, sequence)
    )


@router.post("/sessions/{session_id}/messages", response_model=PlanningSession, status_code=status.HTTP_202_ACCEPTED)
async def message(
    project_name: str,
    session_id: str,
    request: PlanningMessageRequest,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
) -> PlanningSession:
    return _docker_response(lambda: post_message(controller_store, settings, project_name, session_id, request))


@router.post("/sessions/{session_id}/confirm", response_model=PlanningSession, status_code=status.HTTP_202_ACCEPTED)
async def confirm(
    project_name: str,
    session_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
) -> PlanningSession:
    return _docker_response(lambda: confirm_understanding(controller_store, settings, project_name, session_id))


@router.post("/sessions/{session_id}/correct", response_model=PlanningSession, status_code=status.HTTP_202_ACCEPTED)
async def correct(
    project_name: str,
    session_id: str,
    request: PlanningMessageRequest,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
) -> PlanningSession:
    return _docker_response(lambda: correct_understanding(controller_store, settings, project_name, session_id, request))


@router.post("/sessions/{session_id}/proceed", response_model=PlanningSession, status_code=status.HTTP_202_ACCEPTED)
async def proceed(
    project_name: str,
    session_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
) -> PlanningSession:
    return _docker_response(lambda: proceed_without_confirmation(controller_store, settings, project_name, session_id))


@router.post("/sessions/{session_id}/cancel", response_model=PlanningSession)
async def cancel(
    project_name: str,
    session_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> PlanningSession:
    return _docker_response(lambda: cancel_session(controller_store, project_name, session_id))
