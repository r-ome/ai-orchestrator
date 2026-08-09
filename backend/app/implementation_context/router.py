from collections.abc import Callable
from typing import Annotated, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, ContainerError, DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, status

from app import jobs
from app.agents.service import AgentOperationError
from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.implementation_context.config import ContextSettings, get_context_settings
from app.implementation_context.models import (
    GenerateContextRequest,
    ImplementationContext,
)
from app.implementation_context.service import (
    ContextOperationError,
    claim_context,
    execute_context,
    fail_claim,
    session_context,
)
from app.planning.config import PlanningSettings, get_planning_settings
from app.planning.runner import PlanningTurnError


router = APIRouter(
    prefix=(
        "/projects/{project_name}/planning/sessions/{session_id}"
        "/implementation-context"
    ),
    tags=["implementation context"],
)
ResponseType = TypeVar("ResponseType")

StoreDep = Annotated[ControllerStore, Depends(get_controller_store)]


def _response(function: Callable[[], ResponseType]) -> ResponseType:
    try:
        return function()
    except (ContextOperationError, PlanningTurnError, AgentOperationError) as error:
        raise HTTPException(error.status_code, error.detail) from error
    except NotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Docker resource not found") from error
    except ContainerError as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Context helper container failed: {error}",
        ) from error
    except APIError as error:
        response_status = getattr(getattr(error, "response", None), "status_code", 0)
        raise HTTPException(
            response_status or status.HTTP_502_BAD_GATEWAY,
            "Docker rejected the context operation",
        ) from error
    except DockerException as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Docker daemon is unavailable",
        ) from error


@router.post(
    "",
    response_model=ImplementationContext,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_context(
    project_name: str,
    session_id: str,
    request: GenerateContextRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    planning_settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
    settings: Annotated[ContextSettings, Depends(get_context_settings)],
    store: StoreDep,
) -> ImplementationContext:
    """Open the session's context and run its turn in the background.

    202 rather than 201: the turn takes minutes of container time, which no
    browser holds a connection open for. The returned row is `generating`, and
    the reader follows it on the events WebSocket or by polling this route.
    """
    claim = _response(
        lambda: claim_context(
            store,
            planning_settings,
            settings,
            session_id,
            request,
            project_name=project_name,
        )
    )
    jobs.submit_docker_job(
        lambda client: execute_context(client, settings, store, claim),
        name=f"context:{claim.context_id}",
        on_setup_error=lambda detail: fail_claim(store, claim, detail),
    )
    return _session_context(store, session_id, project_name)


@router.get("", response_model=ImplementationContext | None)
def read_context(
    project_name: str,
    session_id: str,
    store: StoreDep,
) -> ImplementationContext | None:
    """The session's context, or null before one has ever been generated.

    Null rather than 404: "this session has no context yet" is the ordinary
    state of every session that has just finished planning, and a caller
    polling for one should not have to read an error to learn that.
    """
    return _response(
        lambda: session_context(store, session_id, project_name=project_name)
    )


def _session_context(
    store: ControllerStore,
    session_id: str,
    project_name: str,
) -> ImplementationContext:
    context = _response(
        lambda: session_context(store, session_id, project_name=project_name)
    )
    if context is None:  # pragma: no cover - the claim above just wrote it
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Implementation context vanished after it was claimed",
        )
    return context
