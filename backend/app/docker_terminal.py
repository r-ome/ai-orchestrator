"""Byte plumbing for Docker exec sockets shared by every terminal endpoint.

`docker.APIClient.exec_start(socket=True)` hands back a raw, blocking socket
wrapper. These helpers hide the two shapes it can take — a `socket`-like object
behind `_sock`, or a plain file object — so callers only see bytes.
"""

from typing import Any

from docker.errors import DockerException


def raw_socket(stream: Any) -> Any:
    return getattr(stream, "_sock", stream)


def read_stream(stream: Any, size: int) -> bytes:
    try:
        socket = raw_socket(stream)
        receive = getattr(socket, "recv", None)
        if callable(receive):
            return receive(size)
        return stream.read(size)
    except OSError as error:
        raise DockerException("Docker terminal socket closed") from error


def write_stream(stream: Any, data: bytes) -> None:
    try:
        socket = raw_socket(stream)
        send_all = getattr(socket, "sendall", None)
        if callable(send_all):
            send_all(data)
            return
        view = memoryview(data)
        while view:
            written = stream.write(view)
            if not written:
                raise DockerException("Docker terminal socket closed")
            view = view[written:]
    except OSError as error:
        raise DockerException("Docker terminal socket closed") from error


def close_stream(stream: Any | None) -> None:
    if stream is None:
        return
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            pass
