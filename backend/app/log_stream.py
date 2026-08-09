"""Reading a live container's output over a WebSocket.

Extracted from `app.previews.router`, which streamed preview container logs
first; the delegation turn endpoints need the same machinery to show what the
assigned model is doing while its turn runs. The socket handling here is
subtle — see the individual docstrings — so it is shared rather than copied.
"""

import asyncio
from collections.abc import Iterable
from socket import SHUT_RDWR
from typing import Any

from docker.errors import DockerException
from fastapi import WebSocket

from app.docker_terminal import close_stream, raw_socket, read_stream


LOG_READ_TIMEOUT_SECONDS = 0.5


class DockerFrameDemuxer:
    """Splits Docker's multiplexed attach stream into stdout/stderr frames.

    Each frame is an 8-byte header (stream type, then a big-endian length)
    followed by that many payload bytes. `read_stream` hands back whatever
    the socket happened to return, which rarely lines up with a frame
    boundary, so this buffers across calls.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while len(self._buffer) >= 8:
            length = int.from_bytes(self._buffer[4:8], "big")
            if len(self._buffer) < 8 + length:
                break
            frames.append(bytes(self._buffer[8 : 8 + length]))
            del self._buffer[: 8 + length]
        return frames


def set_log_read_timeout(stream: Any) -> None:
    """Puts a read deadline on a container's attach socket.

    This is what makes the reader below cancellable. `asyncio.to_thread`
    cannot interrupt the worker thread it dispatched to, and on macOS closing
    a socket from another thread does not wake a `recv()` already blocked on
    it. Without a deadline, a quiet container leaves a worker parked forever;
    `loop.shutdown_default_executor()` then blocks the whole interpreter for
    `asyncio.constants.THREAD_JOIN_TIMEOUT` (300 seconds) at loop close.
    """
    set_timeout = getattr(raw_socket(stream), "settimeout", None)
    if callable(set_timeout):
        set_timeout(LOG_READ_TIMEOUT_SECONDS)


def close_log_stream(stream: Any) -> None:
    """Shuts the attach socket down, then closes it.

    `shutdown()` is the half that reliably wakes a peer blocked in `recv()`;
    `close()` alone does not. The read deadline above already guarantees
    termination, so this is belt and braces that also frees the socket
    promptly rather than one timeout later.
    """
    socket = raw_socket(stream)
    shutdown = getattr(socket, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(SHUT_RDWR)
        except OSError:
            pass
    close_stream(stream)


def read_log_chunk(stream: Any) -> bytes | None:
    """Reads one chunk, returning None when the read deadline simply expired.

    `read_stream` turns every OSError into a DockerException, and a socket
    timeout is an OSError, so an idle container is otherwise indistinguishable
    from a closed stream. Inspecting `__cause__` keeps them apart.
    """
    try:
        return read_stream(stream, 32_768)
    except DockerException as error:
        if isinstance(error.__cause__, TimeoutError):
            return None
        raise


async def forward_container_log(
    websocket: WebSocket,
    container_name: str,
    stream: Any,
) -> None:
    demuxer = DockerFrameDemuxer()
    while True:
        chunk = await asyncio.to_thread(read_log_chunk, stream)
        if chunk is None:
            # Deadline expired with no data. Looping re-enters the `await`
            # above, which is where a cancellation gets delivered.
            continue
        if not chunk:
            break
        for frame in demuxer.feed(chunk):
            if not frame:
                continue
            await websocket.send_json(
                {
                    "type": "log",
                    "container": container_name,
                    "data": frame.decode("utf-8", errors="replace"),
                }
            )


async def cancel_tasks(tasks: Iterable[asyncio.Task]) -> None:
    """Cancels `tasks` and waits for them, preserving our own cancellation.

    `asyncio.gather(..., return_exceptions=True)` is the obvious tool here and
    is wrong. If the coroutine awaiting the gather is itself cancelled while
    the gather is pending, `gather` discards the CancelledError the event loop
    delivered to us and raises a fresh one rebuilt from the last child future
    (`fut._make_cancelled_error()`, asyncio/tasks.py). Children cancelled by a
    bare `task.cancel()` carry no cancel message, so the replacement arrives
    with `args == ()`.

    That matters because anyio only swallows a CancelledError whose message
    starts with "Cancelled via cancel scope" (`is_anyio_cancellation`). The
    message-less replacement escapes the enclosing cancel scope, and under
    Starlette's TestClient that surfaces to the caller as a bare
    `concurrent.futures.CancelledError` out of `websocket_connect`.

    `asyncio.wait` performs no such substitution: a cancellation arriving here
    propagates as the exception the loop actually raised, message intact.
    """
    tasks = list(tasks)
    for task in tasks:
        task.cancel()
    pending = [task for task in tasks if not task.done()]
    if pending:
        await asyncio.wait(pending)
    for task in tasks:
        if task.done() and not task.cancelled():
            # Retrieve any failure so asyncio does not log it as unhandled.
            task.exception()
