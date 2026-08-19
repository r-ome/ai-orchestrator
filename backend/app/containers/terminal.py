"""Interactive shells inside any running container.

Unlike the coding-agent terminal, this opens a *fresh* `docker exec` on every
connection. Nothing persists: closing the socket sends EOF to the shell, the
shell exits, and the next visit starts over from the image's default directory.
"""

from typing import Any

from docker.client import DockerClient
from docker.models.containers import Container

from app.containers.actions import ContainerOperationError

MAX_COLUMNS = 500
MAX_ROWS = 300

# Prefer bash for line editing and completion, but fall back to sh so slim
# images (alpine, distroless-with-busybox) still get a usable prompt.
_SHELL_COMMAND = "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"


def get_container(docker_client: DockerClient, container_id: str) -> Container:
    return docker_client.containers.get(container_id)


def start_container_exec(
    docker_client: DockerClient,
    container: Container,
) -> tuple[str, Any]:
    if container.status != "running":
        raise ContainerOperationError(409, "The container is not running")

    result = docker_client.api.exec_create(
        container.id,
        ["/bin/sh", "-c", _SHELL_COMMAND],
        stdin=True,
        tty=True,
        privileged=False,
        environment={"TERM": "xterm-256color"},
    )
    exec_id = result["Id"]
    stream = docker_client.api.exec_start(
        exec_id,
        detach=False,
        tty=True,
        socket=True,
    )
    return exec_id, stream


def resize_container_exec(
    docker_client: DockerClient,
    exec_id: str,
    *,
    columns: int,
    rows: int,
) -> None:
    docker_client.api.exec_resize(exec_id, height=rows, width=columns)


def inspect_container_exec(docker_client: DockerClient, exec_id: str) -> int | None:
    result = docker_client.api.exec_inspect(exec_id)
    exit_code = result.get("ExitCode")
    return int(exit_code) if exit_code is not None else None
