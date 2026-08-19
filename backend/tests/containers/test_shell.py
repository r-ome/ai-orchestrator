from collections.abc import Iterator
from threading import Event
from typing import Any

import pytest
from docker.errors import NotFound
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.platform.docker_client import get_docker_client

client = TestClient(app)


class StubContainer:
    id = "abc123def456789"
    short_id = "abc123def456"
    name = "example-api"

    def __init__(self, status: str = "running") -> None:
        self.status = status
        self.top_args: dict[str, Any] | None = None

    def top(self, **kwargs: Any) -> dict[str, Any]:
        self.top_args = kwargs
        return {
            "Titles": ["UID", "PID", "PPID", "C", "STIME", "TTY", "TIME", "CMD"],
            "Processes": [
                [
                    "root",
                    "1",
                    "0",
                    "0",
                    "01:00",
                    "?",
                    "00:00:01",
                    "python -m uvicorn app.main:app",
                ],
                ["root", "42", "1", "0", "01:02", "?", "00:00:00", "sh"],
            ],
        }


class StubStream:
    def __init__(self) -> None:
        self.input_received = Event()
        self.output_sent = False
        self.writes: list[bytes] = []
        self.closed = False

    def recv(self, _: int) -> bytes:
        if not self.output_sent:
            self.output_sent = True
            return b"# "
        self.input_received.wait(timeout=2)
        return b""

    def sendall(self, data: bytes) -> None:
        self.writes.append(data)
        self.input_received.set()

    def close(self) -> None:
        self.closed = True
        self.input_received.set()


class StubAPI:
    def __init__(self) -> None:
        self.stream = StubStream()
        self.exec_create_calls: list[tuple[str, list[str], dict[str, Any]]] = []
        self.resize_calls: list[tuple[str, int, int]] = []

    def exec_create(
        self,
        container_id: str,
        command: list[str],
        **kwargs: Any,
    ) -> dict[str, str]:
        self.exec_create_calls.append((container_id, command, kwargs))
        return {"Id": "exec-shell-1"}

    def exec_start(self, exec_id: str, **kwargs: Any) -> StubStream:
        assert exec_id == "exec-shell-1"
        assert kwargs == {"detach": False, "tty": True, "socket": True}
        return self.stream

    def exec_resize(self, exec_id: str, *, height: int, width: int) -> None:
        self.resize_calls.append((exec_id, height, width))

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        assert exec_id == "exec-shell-1"
        return {"ExitCode": 0}


class StubContainerCollection:
    def __init__(self, container: StubContainer) -> None:
        self.container = container

    def get(self, container_id: str) -> StubContainer:
        if container_id not in {
            self.container.id,
            self.container.short_id,
            self.container.name,
        }:
            raise NotFound("container not found")
        return self.container


class StubDockerClient:
    def __init__(self, status: str = "running") -> None:
        self.container = StubContainer(status)
        self.containers = StubContainerCollection(self.container)
        self.api = StubAPI()


def _override(client_instance: StubDockerClient) -> Any:
    def override_docker_client() -> Iterator[StubDockerClient]:
        yield client_instance

    return override_docker_client


def test_list_container_processes() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get("/containers/abc123def456/processes")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["container_id"] == "abc123def456"
    assert body["container_name"] == "example-api"
    assert body["count"] == 2
    assert body["titles"][:2] == ["UID", "PID"]
    assert body["processes"][1] == [
        "root",
        "42",
        "1",
        "0",
        "01:02",
        "?",
        "00:00:00",
        "sh",
    ]


def test_processes_rejected_when_container_is_stopped() -> None:
    docker_client = StubDockerClient(status="exited")
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get("/containers/abc123def456/processes")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert docker_client.container.top_args is None


def test_processes_for_missing_container_returns_not_found() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get("/containers/missing/processes")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_shell_websocket_bridges_a_fresh_exec() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        with client.websocket_connect("/containers/abc123def456/shell") as websocket:
            ready = websocket.receive_json()
            output = websocket.receive_bytes()
            websocket.send_json({"type": "resize", "columns": 120, "rows": 40})
            websocket.send_bytes(b"ls\r")
            exited = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert ready == {
        "type": "ready",
        "container_id": "abc123def456789",
        "exec_id": "exec-shell-1",
        "protocol": "terminal.v1",
    }
    assert output == b"# "
    assert exited == {"type": "exit", "exit_code": 0}
    assert docker_client.api.stream.writes == [b"ls\r"]
    assert docker_client.api.resize_calls == [("exec-shell-1", 40, 120)]

    container_id, command, options = docker_client.api.exec_create_calls[0]
    assert container_id == "abc123def456789"
    assert command[:2] == ["/bin/sh", "-c"]
    assert "exec bash" in command[2]
    assert "exec sh" in command[2]
    assert options["stdin"] is True
    assert options["tty"] is True
    assert options["environment"] == {"TERM": "xterm-256color"}


def test_shell_websocket_rejects_a_stopped_container() -> None:
    docker_client = StubDockerClient(status="exited")
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect("/containers/abc123def456/shell"):
                pass
    finally:
        app.dependency_overrides.clear()

    assert disconnect.value.code == 4409
    assert docker_client.api.exec_create_calls == []


def test_shell_websocket_rejects_a_missing_container() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect("/containers/missing/shell"):
                pass
    finally:
        app.dependency_overrides.clear()

    assert disconnect.value.code == 4404
    assert docker_client.api.exec_create_calls == []
