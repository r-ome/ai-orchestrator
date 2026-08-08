from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, ContainerError, DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.tasks.config import CodingTurnSettings, get_coding_turn_settings
from app.tasks.models import (
    ReportTaskRequest,
    RunTaskRequest,
    StartTaskRequest,
    Task,
    TaskRunResponse,
    TasksResponse,
)
from app.tasks.runner import CodingTurnError
from app.tasks.service import (
    TaskOperationError,
    accept_task,
    get_task,
    list_tasks,
    reject_task,
    report_task_complete,
    run_task,
    start_task,
    verify_task,
)


router = APIRouter(prefix="/tasks", tags=["tasks"])
ResponseType = TypeVar("ResponseType")


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    try:
        return function()
    except (TaskOperationError, CodingTurnError) as error:
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


@router.post("", response_model=Task, status_code=status.HTTP_201_CREATED)
def open_task(
    request: StartTaskRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> Task:
    return _docker_response(
        lambda: start_task(docker_client, controller_store, request)
    )


@router.get("", response_model=TasksResponse)
def get_tasks(
    project_name: Annotated[str, Query(min_length=1, max_length=128)],
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> TasksResponse:
    return _docker_response(
        lambda: list_tasks(docker_client, controller_store, project_name)
    )


@router.get("/{task_id}", response_model=Task)
def get_one_task(
    task_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> Task:
    return _docker_response(lambda: get_task(controller_store, task_id))


@router.post("/{task_id}/report", response_model=Task)
def report_task(
    task_id: str,
    request: ReportTaskRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> Task:
    """The coding agent's completion report. Verified against git before it counts."""
    return _docker_response(
        lambda: report_task_complete(
            docker_client,
            controller_store,
            task_id,
            request,
        )
    )


@router.post("/{task_id}/accept", response_model=Task)
def accept(
    task_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> Task:
    """The human decision to merge. Fast-forward only; 409 on any divergence."""
    return _docker_response(
        lambda: accept_task(docker_client, controller_store, task_id)
    )


@router.post("/{task_id}/reject", response_model=Task)
def reject(
    task_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> Task:
    return _docker_response(
        lambda: reject_task(docker_client, controller_store, task_id)
    )


@router.post("/{task_id}/run", response_model=TaskRunResponse)
def run(
    task_id: str,
    request: RunTaskRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    settings: Annotated[CodingTurnSettings, Depends(get_coding_turn_settings)],
) -> TaskRunResponse:
    """Run one headless coding turn on the task branch.

    Returns rather than raises when the turn ran and produced nothing: the
    caller needs the status, the cost and the tool outcomes to tell a provider
    failure from a turn that simply did not commit.
    """
    return _docker_response(
        lambda: run_task(docker_client, controller_store, settings, task_id, request)
    )


@router.post("/{task_id}/verify", response_model=Task)
def verify(
    task_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> Task:
    """Move a reported task to review without building a preview."""
    return _docker_response(lambda: verify_task(controller_store, task_id))
