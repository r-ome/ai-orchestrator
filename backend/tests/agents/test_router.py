from collections.abc import Iterator
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import NotFound
from fastapi.testclient import TestClient

from app.agents.config import AgentSettings, get_agent_settings
from app.agents.service import (
    LABEL_CREDENTIAL_MANAGED,
    LABEL_CREDENTIAL_PROFILE,
    LABEL_CREDENTIAL_VOLUME,
    LABEL_MANAGED,
    LABEL_PROJECT_VOLUME,
    LABEL_PROVIDER,
    cleanup_agents,
)
from app.docker_client import get_docker_client
from app.main import app
from app.projects.service import ProjectOperationError

client = TestClient(app)


class StubVolume:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.attrs = {"Name": name, "Labels": labels}


class StubVolumes:
    def __init__(self) -> None:
        self.items: dict[str, StubVolume] = {}
        self.create_calls: list[dict[str, Any]] = []

    def get(self, name: str) -> StubVolume:
        try:
            return self.items[name]
        except KeyError as error:
            raise NotFound("volume not found") from error

    def create(self, **kwargs: Any) -> StubVolume:
        self.create_calls.append(kwargs)
        volume = StubVolume(kwargs["name"], kwargs["labels"])
        self.items[volume.name] = volume
        return volume


class StubContainer:
    def __init__(self, create_args: dict[str, Any], number: int) -> None:
        self.id = f"agent-container-{number:04d}"
        self.short_id = self.id[:12]
        self.name = create_args["name"]
        self.status = "created"
        self.start_calls = 0
        self.stop_timeout: int | None = None
        self.remove_force: bool | None = None
        self.exec_run_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.attrs = {
            "Created": f"2026-08-04T10:00:{number:02d}Z",
            "Config": {
                "Image": create_args["image"],
                "Labels": create_args["labels"],
            },
        }

    def start(self) -> None:
        self.start_calls += 1
        self.status = "running"

    def stop(self, *, timeout: int) -> None:
        self.stop_timeout = timeout
        self.status = "exited"

    def remove(self, *, force: bool) -> None:
        self.remove_force = force
        self.status = "removed"

    def exec_run(self, command: list[str], **kwargs: Any) -> None:
        self.exec_run_calls.append((command, kwargs))


class StubContainers:
    def __init__(self) -> None:
        self.items: list[StubContainer] = []
        self.create_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> StubContainer:
        self.create_calls.append(kwargs)
        container = StubContainer(kwargs, len(self.items) + 1)
        self.items.append(container)
        return container

    def list(self, **kwargs: Any) -> list[StubContainer]:
        assert kwargs == {
            "all": True,
            "filters": {"label": f"{LABEL_MANAGED}=true"},
        }
        return [
            container
            for container in self.items
            if (container.attrs["Config"]["Labels"]).get(LABEL_MANAGED) == "true"
            and container.status != "removed"
        ]

    def get(self, agent_id: str) -> StubContainer:
        for container in self.items:
            if agent_id in {container.id, container.short_id, container.name}:
                return container
        raise NotFound("container not found")


class StubStream:
    def __init__(self) -> None:
        self.input_received = Event()
        self.output_sent = False
        self.writes: list[bytes] = []
        self.closed = False

    def recv(self, _: int) -> bytes:
        if not self.output_sent:
            self.output_sent = True
            return b"agent ready\r\n"
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
        return {"Id": "exec-123"}

    def exec_start(self, exec_id: str, **kwargs: Any) -> StubStream:
        assert exec_id == "exec-123"
        assert kwargs == {"detach": False, "tty": True, "socket": True}
        return self.stream

    def exec_resize(self, exec_id: str, *, height: int, width: int) -> None:
        self.resize_calls.append((exec_id, height, width))

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        assert exec_id == "exec-123"
        return {"ExitCode": 0}


class StubDockerClient:
    def __init__(self) -> None:
        self.volumes = StubVolumes()
        self.containers = StubContainers()
        self.api = StubAPI()


SETTINGS = AgentSettings(
    claude_image="test-claude:latest",
    codex_image="test-codex:latest",
)


def _override_docker(instance: StubDockerClient) -> Any:
    def override() -> Iterator[StubDockerClient]:
        yield instance

    return override


def _ready_project() -> SimpleNamespace:
    return SimpleNamespace(
        name="Sample Project",
        volume_name="orchestrator-project-sample",
        ready=True,
    )


@pytest.fixture(autouse=True)
def clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _configure(
    docker_client: StubDockerClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project: SimpleNamespace | None = None,
) -> None:
    app.dependency_overrides[get_docker_client] = _override_docker(docker_client)
    app.dependency_overrides[get_agent_settings] = lambda: SETTINGS
    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        lambda *_: project or _ready_project(),
    )


def test_lists_selectable_agent_providers() -> None:
    app.dependency_overrides[get_agent_settings] = lambda: SETTINGS

    response = client.get("/agents/providers")

    assert response.status_code == 200
    assert response.json() == {
        "providers": [
            {
                "provider": "claude",
                "image": "test-claude:latest",
                "command": ["claude"],
                "credential_directory": "/auth",
                "credential_environment_variable": "CLAUDE_CONFIG_DIR",
            },
            {
                "provider": "codex",
                "image": "test-codex:latest",
                "command": ["codex"],
                "credential_directory": "/auth",
                "credential_environment_variable": "CODEX_HOME",
            },
        ]
    }


def test_rejects_a_second_active_agent_for_the_same_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    _configure(docker_client, monkeypatch)

    first = client.post(
        "/agents",
        json={
            "project_name": "Sample Project",
            "provider": "claude",
            "credential_profile": "personal",
        },
    )
    second = client.post(
        "/agents",
        json={
            "project_name": "Sample Project",
            "provider": "claude",
            "credential_profile": "personal",
        },
    )
    listed = client.get("/agents")

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {
        "detail": "Sandbox already has an active coding agent; use replace explicitly"
    }
    assert listed.json()["count"] == 1
    assert len(docker_client.volumes.create_calls) == 1

    credential_call = docker_client.volumes.create_calls[0]
    assert credential_call["labels"] == {
        LABEL_CREDENTIAL_MANAGED: "true",
        LABEL_PROVIDER: "claude",
        LABEL_CREDENTIAL_PROFILE: "personal",
    }
    credential_volume = credential_call["name"]

    create_call = docker_client.containers.create_calls[0]
    assert create_call["image"] == "test-claude:latest"
    assert create_call["auto_remove"] is True
    assert create_call["read_only"] is True
    assert create_call["cap_drop"] == ["ALL"]
    assert create_call["security_opt"] == ["no-new-privileges:true"]
    assert create_call["working_dir"] == "/workspace"
    assert create_call["environment"] == {
        "CLAUDE_CONFIG_DIR": "/auth",
        "HOME": "/tmp/home",
        "TERM": "xterm-256color",
    }
    assert create_call["volumes"] == {
        "orchestrator-project-sample": {"bind": "/workspace", "mode": "rw"},
        credential_volume: {"bind": "/auth", "mode": "rw"},
    }
    assert create_call["labels"][LABEL_PROJECT_VOLUME] == (
        "orchestrator-project-sample"
    )
    assert create_call["labels"][LABEL_CREDENTIAL_VOLUME] == credential_volume


def test_replaces_an_agent_only_with_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    _configure(docker_client, monkeypatch)
    current = client.post(
        "/agents",
        json={"project_name": "Sample Project", "provider": "claude"},
    ).json()

    rejected = client.post(
        f"/agents/{current['id']}/replace",
        json={"provider": "codex"},
    )
    replaced = client.post(
        f"/agents/{current['id']}/replace",
        json={"provider": "codex", "confirm": True},
    )

    assert rejected.status_code == 400
    assert replaced.status_code == 200
    assert replaced.json()["provider"] == "codex"
    assert replaced.json()["id"] != current["id"]
    assert docker_client.containers.items[0].stop_timeout == 2


def test_codex_uses_a_provider_specific_credential_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    _configure(docker_client, monkeypatch)

    response = client.post(
        "/agents",
        json={"project_name": "Sample Project", "provider": "codex"},
    )

    assert response.status_code == 201
    assert response.json()["provider"] == "codex"
    create_call = docker_client.containers.create_calls[0]
    assert create_call["image"] == "test-codex:latest"
    assert create_call["environment"]["CODEX_HOME"] == "/auth"
    assert response.json()["credential_volume"].startswith(
        "orchestrator-agent-auth-codex-default-"
    )


def test_rejects_an_agent_for_an_unready_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    project = _ready_project()
    project.ready = False
    _configure(docker_client, monkeypatch, project=project)

    response = client.post(
        "/agents",
        json={"project_name": "Sample Project", "provider": "claude"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Project 'Sample Project' is not ready"}
    assert docker_client.containers.create_calls == []
    assert docker_client.volumes.create_calls == []


def test_reports_an_unknown_project(monkeypatch: pytest.MonkeyPatch) -> None:
    docker_client = StubDockerClient()
    _configure(docker_client, monkeypatch)

    def missing_project(*_: Any) -> None:
        raise ProjectOperationError(404, "Project 'Missing' is not registered")

    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        missing_project,
    )

    response = client.post(
        "/agents",
        json={"project_name": "Missing", "provider": "claude"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Project 'Missing' is not registered"}


def test_stop_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    docker_client = StubDockerClient()
    _configure(docker_client, monkeypatch)
    created = client.post(
        "/agents",
        json={"project_name": "Sample Project", "provider": "claude"},
    ).json()

    rejected = client.post(f"/agents/{created['id']}/stop", json={})
    stopped = client.post(
        f"/agents/{created['id']}/stop",
        json={"confirm": True, "timeout_seconds": 5},
    )

    assert rejected.status_code == 400
    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    assert docker_client.containers.items[0].stop_timeout == 5


def test_cleanup_removes_running_and_stopped_agents() -> None:
    docker_client = StubDockerClient()
    labels = {LABEL_MANAGED: "true", LABEL_PROVIDER: "claude"}
    first = docker_client.containers.create(
        name="first",
        image="test",
        labels=labels,
    )
    second = docker_client.containers.create(
        name="second",
        image="test",
        labels=labels,
    )
    first.status = "running"
    second.status = "exited"

    result = cleanup_agents(docker_client)

    assert result.removed_count == 2
    assert first.stop_timeout == 2
    assert second.remove_force is True


def test_websocket_bridges_terminal_and_keeps_container_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    _configure(docker_client, monkeypatch)
    created = client.post(
        "/agents",
        json={"project_name": "Sample Project", "provider": "claude"},
    ).json()

    with client.websocket_connect(created["websocket_url"]) as websocket:
        ready = websocket.receive_json()
        output = websocket.receive_bytes()
        websocket.send_json({"type": "resize", "columns": 120, "rows": 40})
        websocket.send_bytes(b"hello Claude\r")
        exited = websocket.receive_json()

    assert ready == {
        "type": "ready",
        "agent_id": created["id"],
        "exec_id": "exec-123",
        "protocol": "terminal.v1",
    }
    assert output == b"agent ready\r\n"
    assert exited == {"type": "exit", "exit_code": 0}
    assert docker_client.api.stream.writes == [b"hello Claude\r"]
    assert docker_client.api.resize_calls == [("exec-123", 40, 120)]
    exec_call = docker_client.api.exec_create_calls[0]
    assert exec_call[1][:2] == ["sh", "-lc"]
    assert "tmux has-session -t agent" in exec_call[1][2]
    assert "tmux new-session -d -s agent 'exec claude'" in exec_call[1][2]
    assert exec_call[2]["workdir"] == "/workspace"
    container = docker_client.containers.items[0]
    assert container.exec_run_calls == [
        (
            ["tmux", "detach-client", "-s", "agent"],
            {"stdout": False, "stderr": False},
        )
    ]
    assert container.stop_timeout is None
    assert container.status == "running"
