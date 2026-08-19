import asyncio
from collections.abc import Iterable
from socket import SHUT_RDWR
from typing import Annotated, Any, Callable, TypeVar

from docker.client import DockerClient
from docker.errors import DockerException
from fastapi import APIRouter, Body, Depends, HTTPException, WebSocket, status
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.docker_errors import (
    DOCKER_DAEMON_UNAVAILABLE_DETAIL,
    DockerErrorPolicy,
    PassThroughApiError,
    docker_response,
)
from app.log_stream import (
    DockerFrameDemuxer,
    LOG_READ_TIMEOUT_SECONDS,
    cancel_tasks,
    close_log_stream,
    forward_container_log,
    read_log_chunk,
    set_log_read_timeout,
)
from app.previews.config import PreviewSettings, get_preview_settings
from app.previews.errors import PreviewOperationError
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
from app.projects.secrets import (
    delete_project_secret,
    get_project_secrets,
    import_project_secrets,
    set_project_secrets,
)
from app.previews.service import (
    database_sharing_state,
    get_current_preview,
    open_preview_log_stream,
    preview_creation_logs,
    preview_logs,
    preview_running_containers,
    propose_preview,
    require_preview_proposal,
    restart_preview,
    reuse_preview,
    start_preview,
    stop_preview,
)


router = APIRouter(prefix="/projects/{project_name}", tags=["previews"])
ResponseType = TypeVar("ResponseType")
_active_event_sessions: set[str] = set()
_active_event_sessions_lock = asyncio.Lock()
_EVENT_POLL_SECONDS = 0.5
_TERMINAL_STATUSES = {"running", "failed"}
LOG_READ_TIMEOUT_SECONDS = 0.5


_DOCKER_ERRORS = DockerErrorPolicy(
    domain_errors=(PreviewOperationError,),
    container_error_detail="Preview helper container failed",
    api_error=PassThroughApiError("Docker rejected the preview operation"),
)


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    return docker_response(function, _DOCKER_ERRORS)


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


def _progress_message(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return {
        "type": "progress",
        "id": int(event["id"]),
        "created_at": str(event["created_at"]),
        "level": str(payload.get("level") or "info"),
        "step": str(payload.get("step") or "preview"),
        "message": str(payload.get("message") or ""),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "duration_ms": payload.get("duration_ms"),
    }


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    """Resolves once the client disconnects.

    The streaming loop below never itself calls `websocket.receive()` — it
    only sends — so nothing would otherwise notice a client-initiated close:
    `websocket.client_state` is only updated by Starlette when a message is
    actually received. Running this concurrently is what makes a disconnect
    observable at all, mirroring `agent_terminal`'s paired input/output
    tasks in `agents/router.py`.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


async def _stream_progress_and_logs(
    websocket: WebSocket,
    docker_client: DockerClient,
    controller_store: ControllerStore,
    proposal_id: str,
    log_streams: dict[str, Any],
    log_tasks: dict[str, asyncio.Task],
) -> None:
    streamed_containers: set[str] = set()
    last_id = 0
    events = await asyncio.to_thread(
        controller_store.events_for_run, proposal_id, kind="preview.progress"
    )
    for event in events:
        await websocket.send_json(_progress_message(event))
        last_id = max(last_id, int(event["id"]))

    while True:
        events = await asyncio.to_thread(
            controller_store.events_for_run, proposal_id, kind="preview.progress"
        )
        for event in events:
            if int(event["id"]) <= last_id:
                continue
            await websocket.send_json(_progress_message(event))
            last_id = max(last_id, int(event["id"]))
        status_value = ""
        preview_id = ""
        if events:
            latest = events[-1].get("payload") or {}
            status_value = str(latest.get("status") or "")
            preview_id = str(latest.get("preview_id") or "")
        if preview_id:
            try:
                containers = await asyncio.to_thread(
                    preview_running_containers, docker_client, preview_id
                )
            except DockerException:
                containers = []
            for container in containers:
                if container.name in streamed_containers:
                    continue
                streamed_containers.add(container.name)
                try:
                    stream = await asyncio.to_thread(
                        open_preview_log_stream, docker_client, container
                    )
                except DockerException:
                    continue
                set_log_read_timeout(stream)
                log_streams[container.name] = stream
                log_tasks[container.name] = asyncio.create_task(
                    forward_container_log(websocket, container.name, stream)
                )
        if status_value in _TERMINAL_STATUSES and not log_tasks:
            # A terminal progress status ends the session only while nothing is
            # streaming container logs. Once a log stream is attached the
            # session's job is no longer "report progress until the preview is
            # up" but "carry those logs", so the client decides when to leave —
            # the same contract `agent_terminal` has. Returning here regardless
            # would close the socket within milliseconds of attaching the log
            # stream, because "running" is recorded at almost the same moment
            # the container starts producing output.
            return
        await asyncio.sleep(_EVENT_POLL_SECONDS)


@router.websocket("/preview-proposals/{proposal_id}/events")
async def preview_events(
    websocket: WebSocket,
    project_name: str,
    proposal_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> None:
    try:
        await asyncio.to_thread(
            require_preview_proposal,
            docker_client,
            controller_store,
            project_name,
            proposal_id,
        )
    except PreviewOperationError as error:
        await websocket.close(code=4404, reason=error.detail)
        return
    except DockerException:
        await websocket.close(code=4503, reason=DOCKER_DAEMON_UNAVAILABLE_DETAIL)
        return

    session_key = f"{project_name}:{proposal_id}"
    async with _active_event_sessions_lock:
        if session_key in _active_event_sessions:
            await websocket.close(code=4409, reason="Proposal already has an events stream")
            return
        _active_event_sessions.add(session_key)

    accepted = False
    log_streams: dict[str, Any] = {}
    log_tasks: dict[str, asyncio.Task] = {}
    session_tasks: list[asyncio.Task] = []
    stream_task: asyncio.Task | None = None
    try:
        await websocket.accept()
        accepted = True
        session_tasks.append(asyncio.create_task(_watch_for_disconnect(websocket)))
        stream_task = asyncio.create_task(
            _stream_progress_and_logs(
                websocket,
                docker_client,
                controller_store,
                proposal_id,
                log_streams,
                log_tasks,
            )
        )
        session_tasks.append(stream_task)
        # Every task this endpoint owns is in `session_tasks` or `log_tasks`
        # before it is awaited, so the `finally` below tears all of them down
        # even if this coroutine is cancelled inside the wait itself.
        done, _pending = await asyncio.wait(
            session_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stream_task in done and not stream_task.cancelled():
            # Surface a genuine streaming failure; a cancelled task has no
            # result to read and `.result()` would re-raise the cancellation.
            stream_task.result()
    except WebSocketDisconnect:
        pass
    except DockerException:
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json(
                {"type": "error", "detail": DOCKER_DAEMON_UNAVAILABLE_DETAIL}
            )
    finally:
        # The session slot and every open stream are released before the
        # awaits below, so a cancellation racing this teardown (a client
        # tearing down at the same moment the loop reaches a terminal
        # status, say) can never leave the proposal permanently marked as
        # "already streaming" or leak an open Docker socket.
        async with _active_event_sessions_lock:
            _active_event_sessions.discard(session_key)
        # Closing the streams first is load-bearing. `forward_container_log`
        # parks in `asyncio.to_thread(read_stream, ...)`, and cancelling a task
        # does not interrupt the worker thread it is blocked on. Only closing
        # the underlying Docker socket makes that read return, which is what
        # lets the wait below finish instead of hanging forever.
        for stream in log_streams.values():
            close_log_stream(stream)
        await cancel_tasks([*session_tasks, *log_tasks.values()])
        if accepted and websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1000)
