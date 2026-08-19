"""Live progress and container output for one claimed turn.

The shape follows `app.previews.router`'s preview event socket: replay the
progress already stored, then keep polling for new progress while attaching a
follow stream to any container the turn starts. A reader watching a work item
therefore sees both the controller's own milestones and the assigned model's
output as it is produced.
"""

import asyncio
from functools import partial
from typing import Annotated, Any

from docker.client import DockerClient
from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.controller.store import ControllerStore, get_controller_store
from app.platform.docker_client import get_docker_client
from app.platform.log_stream import (
    cancel_tasks,
    close_log_stream,
    forward_container_log,
    set_log_read_timeout,
)
from app.previews.service import open_preview_log_stream
from app.turns.locators import (
    TERMINAL_STEPS,
    TurnLocator,
    TurnNotFound,
    locate,
    running_containers,
)

router = APIRouter(tags=["turns"])

_EVENT_POLL_SECONDS = 0.5


def _progress_message(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return {
        "type": "progress",
        "id": int(event["id"]),
        "created_at": str(event["created_at"]),
        "level": str(payload.get("level") or "info"),
        "step": str(payload.get("step") or ""),
        "message": str(payload.get("message") or ""),
    }


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    """Resolves once the client disconnects.

    The streaming loop only sends, so nothing else would notice a
    client-initiated close. See the same helper in `app.previews.router`.
    """
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


async def _stream(
    websocket: WebSocket,
    docker_client: DockerClient,
    store: ControllerStore,
    job_id: str,
    locator: TurnLocator,
    log_streams: dict[str, Any],
    log_tasks: dict[str, asyncio.Task],
) -> None:
    streamed: set[str] = set()
    last_id = 0
    finished = False

    while True:
        events = await asyncio.to_thread(
            partial(store.events_for_run, job_id, kind=locator.event_kind)
        )
        for event in events:
            if int(event["id"]) <= last_id:
                continue
            await websocket.send_json(_progress_message(event))
            last_id = max(last_id, int(event["id"]))
            step = str((event.get("payload") or {}).get("step") or "")
            if step in TERMINAL_STEPS:
                finished = True

        for container in await asyncio.to_thread(
            running_containers, docker_client, locator
        ):
            if container.name in streamed:
                continue
            streamed.add(container.name)
            try:
                stream = await asyncio.to_thread(
                    open_preview_log_stream, docker_client, container
                )
            except Exception:
                continue
            set_log_read_timeout(stream)
            log_streams[container.name] = stream
            log_tasks[container.name] = asyncio.create_task(
                forward_container_log(websocket, container.name, stream)
            )

        for name, task in list(log_tasks.items()):
            if task.done():
                log_tasks.pop(name, None)
                stream = log_streams.pop(name, None)
                if stream is not None:
                    await asyncio.to_thread(close_log_stream, stream)

        if finished and not log_tasks:
            # A terminal step ends the session only once nothing is still
            # carrying container output, so the tail of a turn's last words is
            # not cut off by the settle event racing ahead of it.
            await websocket.send_json({"type": "end"})
            return
        await asyncio.sleep(_EVENT_POLL_SECONDS)


@router.websocket(
    "/projects/{project_name}/planning/sessions/{session_id}"
    "/turns/{kind}/{job_id}/events"
)
async def turn_events(
    websocket: WebSocket,
    project_name: str,
    session_id: str,
    kind: str,
    job_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> None:
    try:
        locator = await asyncio.to_thread(
            locate, store, kind, job_id, session_id=session_id
        )
    except TurnNotFound as error:
        await websocket.close(code=4404, reason=str(error))
        return

    log_streams: dict[str, Any] = {}
    log_tasks: dict[str, asyncio.Task] = {}
    session_tasks: list[asyncio.Task] = []
    accepted = False
    try:
        await websocket.accept()
        accepted = True
        session_tasks.append(asyncio.create_task(_watch_for_disconnect(websocket)))
        session_tasks.append(
            asyncio.create_task(
                _stream(
                    websocket,
                    docker_client,
                    store,
                    job_id,
                    locator,
                    log_streams,
                    log_tasks,
                )
            )
        )
        await asyncio.wait(session_tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        await cancel_tasks([*session_tasks, *log_tasks.values()])
        for stream in log_streams.values():
            await asyncio.to_thread(close_log_stream, stream)
        if accepted and websocket.client_state is not WebSocketState.DISCONNECTED:
            await websocket.close()
