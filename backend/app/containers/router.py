import asyncio
import json
from typing import Annotated, Any, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.containers.actions import (
    MAX_FILE_BYTES,
    ContainerOperationError,
    inspect_managed_container,
    list_container_processes,
    prune_managed_containers,
    read_container_file,
    remove_managed_container,
    stop_managed_container,
)
from app.containers.models import (
    AllContainersResponse,
    ConfirmAction,
    ContainerDetails,
    ContainerFileResponse,
    ContainerProcessesResponse,
    ContainerStatusResponse,
    PruneContainersResponse,
    RemoveContainerResponse,
    RunningContainersResponse,
    StopContainerAction,
    StopContainerResponse,
)
from app.containers.service import (
    get_running_container_status,
    list_all_containers,
    list_running_containers,
)
from app.containers.terminal import (
    MAX_COLUMNS,
    MAX_ROWS,
    get_container,
    inspect_container_exec,
    resize_container_exec,
    start_container_exec,
)
from app.platform.docker_client import get_docker_client
from app.platform.docker_errors import (
    DOCKER_DAEMON_UNAVAILABLE_DETAIL,
    ConflictApiError,
    DockerErrorPolicy,
    docker_response,
)
from app.platform.docker_terminal import close_stream, read_stream, write_stream

router = APIRouter(prefix="/containers", tags=["containers"])
ResponseType = TypeVar("ResponseType")


_DOCKER_ERRORS = DockerErrorPolicy(
    domain_errors=(ContainerOperationError,),
    api_error=ConflictApiError(
        "Docker rejected the action because the resource is running"
    ),
)


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    return docker_response(function, _DOCKER_ERRORS)


@router.get("", response_model=RunningContainersResponse)
def get_running_containers(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> RunningContainersResponse:
    return _docker_response(lambda: list_running_containers(docker_client))


@router.get("/status", response_model=ContainerStatusResponse)
def get_container_status(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ContainerStatusResponse:
    return _docker_response(lambda: get_running_container_status(docker_client))


@router.get("/all", response_model=AllContainersResponse)
def get_all_containers(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> AllContainersResponse:
    return _docker_response(lambda: list_all_containers(docker_client))


@router.post("/prune", response_model=PruneContainersResponse)
def prune_containers(
    request: ConfirmAction,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> PruneContainersResponse:
    _require_confirmation(request.confirm)
    return _docker_response(lambda: prune_managed_containers(docker_client))


@router.get("/{container_id}", response_model=ContainerDetails)
def inspect_container(
    container_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ContainerDetails:
    return _docker_response(
        lambda: inspect_managed_container(docker_client, container_id)
    )


@router.delete("/{container_id}", response_model=RemoveContainerResponse)
def remove_container(
    container_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    confirm: Annotated[bool, Query(description="Confirm permanent removal")] = False,
    force: Annotated[bool, Query(description="Force-remove a running container")] = False,
    remove_volumes: Annotated[
        bool,
        Query(description="Remove attached anonymous volumes"),
    ] = False,
) -> RemoveContainerResponse:
    _require_confirmation(confirm)
    return _docker_response(
        lambda: remove_managed_container(
            docker_client,
            container_id,
            force=force,
            remove_volumes=remove_volumes,
        )
    )


@router.post("/{container_id}/stop", response_model=StopContainerResponse)
def stop_container(
    container_id: str,
    request: StopContainerAction,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> StopContainerResponse:
    _require_confirmation(request.confirm)
    if not 1 <= request.timeout_seconds <= 60:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="timeout_seconds must be between 1 and 60",
        )
    return _docker_response(
        lambda: stop_managed_container(
            docker_client,
            container_id,
            timeout_seconds=request.timeout_seconds,
        )
    )


@router.get("/{container_id}/files", response_model=ContainerFileResponse)
def get_container_file(
    container_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    path: Annotated[str, Query(min_length=1)],
    max_bytes: Annotated[int, Query(ge=1, le=MAX_FILE_BYTES)] = 65_536,
) -> ContainerFileResponse:
    return _docker_response(
        lambda: read_container_file(
            docker_client,
            container_id,
            path,
            max_bytes=max_bytes,
        )
    )


@router.get("/{container_id}/processes", response_model=ContainerProcessesResponse)
def get_container_processes(
    container_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> ContainerProcessesResponse:
    return _docker_response(
        lambda: list_container_processes(docker_client, container_id)
    )


@router.websocket("/{container_id}/shell")
async def container_shell(
    websocket: WebSocket,
    container_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
) -> None:
    """A throwaway interactive shell. Each connection runs its own exec, so
    several tabs on the same container never fight over one session."""
    try:
        container = await asyncio.to_thread(get_container, docker_client, container_id)
    except NotFound:
        await websocket.close(code=4404, reason="Container not found")
        return
    except DockerException:
        await websocket.close(code=4503, reason=DOCKER_DAEMON_UNAVAILABLE_DETAIL)
        return

    if container.status != "running":
        await websocket.close(code=4409, reason="Container is not running")
        return

    stream: Any | None = None
    accepted = False
    try:
        await websocket.accept()
        accepted = True
        exec_id, stream = await asyncio.to_thread(
            start_container_exec,
            docker_client,
            container,
        )
        await websocket.send_json(
            {
                "type": "ready",
                "container_id": container.id,
                "exec_id": exec_id,
                "protocol": "terminal.v1",
            }
        )

        output_task = asyncio.create_task(
            _forward_shell_output(websocket, docker_client, exec_id, stream)
        )
        input_task = asyncio.create_task(
            _forward_shell_input(websocket, docker_client, exec_id, stream)
        )
        completed, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        # Closing the socket sends EOF to the shell, which then exits.
        close_stream(stream)
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*completed)
    except WebSocketDisconnect:
        pass
    except ContainerOperationError as error:
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({"type": "error", "detail": error.detail})
    except DockerException:
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(
                {"type": "error", "detail": DOCKER_DAEMON_UNAVAILABLE_DETAIL}
            )
    finally:
        close_stream(stream)
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1000)


async def _forward_shell_output(
    websocket: WebSocket,
    docker_client: DockerClient,
    exec_id: str,
    stream: Any,
) -> None:
    while True:
        chunk = await asyncio.to_thread(read_stream, stream, 32_768)
        if not chunk:
            break
        await websocket.send_bytes(chunk)
    exit_code = await asyncio.to_thread(inspect_container_exec, docker_client, exec_id)
    await websocket.send_json({"type": "exit", "exit_code": exit_code})


async def _forward_shell_input(
    websocket: WebSocket,
    docker_client: DockerClient,
    exec_id: str,
    stream: Any,
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        binary = message.get("bytes")
        if binary is not None:
            await _write_shell_input(websocket, stream, binary)
            continue
        text = message.get("text")
        if text is None:
            continue
        try:
            control = json.loads(text)
        except json.JSONDecodeError:
            await _shell_protocol_error(websocket, "Text frames must contain JSON")
            continue
        message_type = control.get("type")
        if message_type == "input":
            data = control.get("data")
            if not isinstance(data, str):
                await _shell_protocol_error(websocket, "input.data must be a string")
                continue
            await _write_shell_input(websocket, stream, data.encode())
        elif message_type == "resize":
            columns = control.get("columns")
            rows = control.get("rows")
            if not (
                isinstance(columns, int)
                and isinstance(rows, int)
                and 1 <= columns <= MAX_COLUMNS
                and 1 <= rows <= MAX_ROWS
            ):
                await _shell_protocol_error(
                    websocket,
                    f"resize requires columns 1..{MAX_COLUMNS} "
                    f"and rows 1..{MAX_ROWS}",
                )
                continue
            await asyncio.to_thread(
                resize_container_exec,
                docker_client,
                exec_id,
                columns=columns,
                rows=rows,
            )
        elif message_type == "close":
            return
        else:
            await _shell_protocol_error(websocket, "Unknown terminal message type")


async def _write_shell_input(
    websocket: WebSocket,
    stream: Any,
    data: bytes,
) -> None:
    if len(data) > 65_536:
        await _shell_protocol_error(websocket, "Input frame exceeds 65536 bytes")
        return
    await asyncio.to_thread(write_stream, stream, data)


async def _shell_protocol_error(websocket: WebSocket, detail: str) -> None:
    await websocket.send_json({"type": "error", "detail": detail})


def _require_confirmation(confirm: bool) -> None:
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set confirm=true to run this destructive action",
        )
