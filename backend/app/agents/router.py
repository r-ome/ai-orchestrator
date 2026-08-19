import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from typing import Annotated, Any, TypeVar
from uuid import uuid4

from docker.client import DockerClient
from docker.errors import DockerException
from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.agents.config import AgentSettings, get_agent_settings
from app.agents.models import (
    AgentProvidersResponse,
    CodingAgent,
    CodingAgentsResponse,
    CreateAgentRequest,
    ReplaceAgentRequest,
    StopAgentRequest,
    StopAgentResponse,
)
from app.agents.service import (
    AgentOperationError,
    create_agent,
    detach_agent_terminal,
    get_managed_agent_container,
    inspect_agent,
    inspect_agent_exec,
    list_agents,
    replace_agent,
    resize_agent_exec,
    start_agent_exec,
    stop_agent,
)
from app.controller.store import (
    AgentWriterSessionExists,
    ControllerStore,
    SandboxWriterAdmissionError,
    get_controller_store,
)
from app.platform.docker_client import get_docker_client
from app.platform.docker_errors import (
    DOCKER_DAEMON_UNAVAILABLE_DETAIL,
    ConflictApiError,
    DockerErrorPolicy,
    docker_response,
)
from app.platform.docker_terminal import close_stream, read_stream, write_stream
from app.platform.labels import LABEL_RUN_ID, LABEL_SANDBOX_ID

router = APIRouter(prefix="/agents", tags=["agents"])
ResponseType = TypeVar("ResponseType")
_active_sessions: set[str] = set()
_active_sessions_lock = asyncio.Lock()
_WRITER_HEARTBEAT_SECONDS = 15


_DOCKER_ERRORS = DockerErrorPolicy(
    domain_errors=(AgentOperationError,),
    api_error=ConflictApiError(
        "Docker rejected the action because the resource conflicts"
    ),
)


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    return docker_response(function, _DOCKER_ERRORS)


@router.get("/providers", response_model=AgentProvidersResponse)
def get_agent_providers(
    settings: Annotated[AgentSettings, Depends(get_agent_settings)],
) -> AgentProvidersResponse:
    return AgentProvidersResponse(
        providers=[provider.details() for provider in settings.providers()]
    )


@router.post("", response_model=CodingAgent, status_code=status.HTTP_201_CREATED)
def summon_agent(
    request: CreateAgentRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[AgentSettings, Depends(get_agent_settings)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> CodingAgent:
    return _docker_response(
        lambda: create_agent(docker_client, settings, request, controller_store)
    )


@router.get("", response_model=CodingAgentsResponse)
def get_agents(
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[AgentSettings, Depends(get_agent_settings)],
) -> CodingAgentsResponse:
    return _docker_response(lambda: list_agents(docker_client, settings))


@router.get("/{agent_id}", response_model=CodingAgent)
def get_agent(
    agent_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[AgentSettings, Depends(get_agent_settings)],
) -> CodingAgent:
    return _docker_response(lambda: inspect_agent(docker_client, settings, agent_id))


@router.post("/{agent_id}/stop", response_model=StopAgentResponse)
def stop_coding_agent(
    agent_id: str,
    request: StopAgentRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> StopAgentResponse:
    if not request.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set confirm=true to stop this agent",
        )
    return _docker_response(
        lambda: stop_agent(
            docker_client,
            agent_id,
            timeout_seconds=request.timeout_seconds,
            controller_store=controller_store,
        )
    )


@router.post("/{agent_id}/replace", response_model=CodingAgent)
def replace_coding_agent(
    agent_id: str,
    request: ReplaceAgentRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[AgentSettings, Depends(get_agent_settings)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> CodingAgent:
    if not request.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set confirm=true to replace this agent",
        )
    return _docker_response(
        lambda: replace_agent(
            docker_client,
            settings,
            agent_id,
            request,
            controller_store,
        )
    )


@router.websocket("/{agent_id}/ws")
async def agent_terminal(
    websocket: WebSocket,
    agent_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[AgentSettings, Depends(get_agent_settings)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> None:
    try:
        container = get_managed_agent_container(docker_client, agent_id)
    except AgentOperationError as error:
        await websocket.close(code=4404, reason=error.detail)
        return
    except DockerException:
        await websocket.close(code=4503, reason=DOCKER_DAEMON_UNAVAILABLE_DETAIL)
        return

    canonical_id = container.id
    async with _active_sessions_lock:
        if canonical_id in _active_sessions:
            await websocket.close(code=4409, reason="Agent already has a terminal")
            return
        _active_sessions.add(canonical_id)

    stream: Any | None = None
    terminal_started = False
    accepted = False
    writer_session_id = uuid4().hex
    writer_session_open = False
    heartbeat_task: asyncio.Task[None] | None = None
    try:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        sandbox_id = str(labels.get(LABEL_SANDBOX_ID) or "")
        agent_run_id = str(labels.get(LABEL_RUN_ID) or "")
        if not sandbox_id or not agent_run_id:
            await websocket.close(
                code=4500,
                reason="Agent controller labels are incomplete",
            )
            return
        try:
            # A writable attached terminal counts for its whole lifetime. The
            # controller cannot observe whether a human is idle or typing.
            controller_store.open_agent_writer_session(
                session_id=writer_session_id,
                sandbox_id=sandbox_id,
                agent_run_id=agent_run_id,
                kind="terminal",
            )
        except SandboxWriterAdmissionError as error:
            await websocket.close(code=4409, reason=str(error))
            return
        except AgentWriterSessionExists:
            await websocket.close(code=4409, reason="Agent already has a terminal")
            return
        writer_session_open = True

        await websocket.accept()
        accepted = True
        heartbeat_task = asyncio.create_task(
            _heartbeat_writer_session(controller_store, writer_session_id)
        )
        exec_id, stream = await asyncio.to_thread(
            start_agent_exec,
            docker_client,
            container,
            settings,
        )
        terminal_started = True
        await websocket.send_json(
            {
                "type": "ready",
                "agent_id": canonical_id,
                "exec_id": exec_id,
                "protocol": "terminal.v1",
            }
        )

        output_task = asyncio.create_task(
            _forward_agent_output(websocket, docker_client, exec_id, stream)
        )
        input_task = asyncio.create_task(
            _forward_client_input(websocket, docker_client, exec_id, stream)
        )
        completed, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.to_thread(detach_agent_terminal, container)
        terminal_started = False
        close_stream(stream)
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*completed)
    except WebSocketDisconnect:
        pass
    except AgentOperationError as error:
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({"type": "error", "detail": error.detail})
    except DockerException:
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(
                {"type": "error", "detail": DOCKER_DAEMON_UNAVAILABLE_DETAIL}
            )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if terminal_started:
            try:
                await asyncio.to_thread(detach_agent_terminal, container)
            except DockerException:
                pass
        close_stream(stream)
        if writer_session_open:
            with suppress(Exception):
                controller_store.close_agent_writer_session(writer_session_id)
        async with _active_sessions_lock:
            _active_sessions.discard(canonical_id)
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1000)


async def _heartbeat_writer_session(
    controller_store: ControllerStore,
    session_id: str,
) -> None:
    while True:
        await asyncio.sleep(_WRITER_HEARTBEAT_SECONDS)
        open_session = await asyncio.to_thread(
            controller_store.heartbeat_agent_writer_session,
            session_id,
        )
        if not open_session:
            return


async def _forward_agent_output(
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
    exit_code = await asyncio.to_thread(inspect_agent_exec, docker_client, exec_id)
    await websocket.send_json({"type": "exit", "exit_code": exit_code})


async def _forward_client_input(
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
            await _write_input(websocket, stream, binary)
            continue
        text = message.get("text")
        if text is None:
            continue
        try:
            control = json.loads(text)
        except json.JSONDecodeError:
            await _protocol_error(websocket, "Text frames must contain JSON")
            continue
        message_type = control.get("type")
        if message_type == "input":
            data = control.get("data")
            if not isinstance(data, str):
                await _protocol_error(websocket, "input.data must be a string")
                continue
            await _write_input(websocket, stream, data.encode())
        elif message_type == "resize":
            columns = control.get("columns")
            rows = control.get("rows")
            if not (
                isinstance(columns, int)
                and isinstance(rows, int)
                and 1 <= columns <= 500
                and 1 <= rows <= 300
            ):
                await _protocol_error(
                    websocket,
                    "resize requires columns 1..500 and rows 1..300",
                )
                continue
            await asyncio.to_thread(
                resize_agent_exec,
                docker_client,
                exec_id,
                columns=columns,
                rows=rows,
            )
        elif message_type == "close":
            return
        else:
            await _protocol_error(websocket, "Unknown terminal message type")


async def _write_input(websocket: WebSocket, stream: Any, data: bytes) -> None:
    if len(data) > 65_536:
        await _protocol_error(websocket, "Input frame exceeds 65536 bytes")
        return
    await asyncio.to_thread(write_stream, stream, data)


async def _protocol_error(websocket: WebSocket, detail: str) -> None:
    await websocket.send_json({"type": "error", "detail": detail})
