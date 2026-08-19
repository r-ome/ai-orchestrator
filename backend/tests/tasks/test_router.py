from typing import Any

import pytest
from conftest import register_ready_v1_sandbox
from fastapi.testclient import TestClient

from app.controller.store import get_controller_store
from app.main import app
from app.platform.docker_client import get_docker_client
from app.projects.models import ProjectRegistration
from app.tasks.models import TaskStatus
from app.tasks.service import transition_task

BASE_COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40


class _StubContainers:
    def __init__(self) -> None:
        self.report_output = b""
        self.settle_output = b""

    def create(self, **kwargs: Any) -> "_Container":
        script = kwargs["command"][0]
        if "git switch -c" in script:
            return _Container(f"base-branch main\n{BASE_COMMIT}\n".encode())
        if "result branch-moved" in script or "result switch-failed" in script:
            return _Container(self.settle_output)
        return _Container(self.report_output)


class _Container:
    def __init__(self, output: bytes) -> None:
        self.output = output

    def start(self) -> None:
        pass

    def wait(self, *, timeout: int) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        return self.output if stdout else b""

    def remove(self, *, force: bool) -> None:
        pass


class _StubDockerClient:
    def __init__(self) -> None:
        self.containers = _StubContainers()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    docker_client = _StubDockerClient()
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="managed:project-1",
        volume_name="orchestrator-project-sample",
        created_at="2026-01-01T00:00:00Z",
        ready=True,
    )
    monkeypatch.setattr(
        "app.tasks.service.inspect_registered_project",
        lambda *_: project,
    )
    monkeypatch.setattr(
        "app.tasks.service.ensure_git_baseline",
        lambda *_: BASE_COMMIT,
    )
    store = get_controller_store()
    register_ready_v1_sandbox(
        store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
    )
    app.dependency_overrides[get_docker_client] = lambda: docker_client
    with TestClient(app) as test_client:
        yield test_client, docker_client
    app.dependency_overrides.clear()


def test_task_endpoints_cover_start_list_and_report(client: Any) -> None:
    test_client, docker_client = client

    created = test_client.post(
        "/tasks",
        json={"project_name": "Sample Project", "title": "Add a contact page"},
    )
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == TaskStatus.OPEN.value

    conflict = test_client.post("/tasks", json={"project_name": "Sample Project"})
    assert conflict.status_code == 409
    assert "already has an open task" in conflict.json()["detail"]

    listed = test_client.get("/tasks", params={"project_name": "Sample Project"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    docker_client.containers.report_output = (
        f"head {BASE_COMMIT}\nbranch {task['branch']}\ndirty ?? .agent/preview.yaml\n"
    ).encode()
    dirty = test_client.post(f"/tasks/{task['id']}/report", json={"summary": "done"})
    assert dirty.status_code == 409
    assert ".agent/preview.yaml" in dirty.json()["detail"]

    docker_client.containers.report_output = (
        f"head {NEXT_COMMIT}\nbranch {task['branch']}\n"
    ).encode()
    reported = test_client.post(f"/tasks/{task['id']}/report", json={})
    assert reported.status_code == 200
    assert reported.json()["status"] == TaskStatus.REPORTED.value
    assert reported.json()["head_commit"] == NEXT_COMMIT

    fetched = test_client.get(f"/tasks/{task['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == TaskStatus.REPORTED.value

    assert test_client.get("/tasks/not-a-task-id").status_code == 404


def test_accept_and_reject_endpoints(client: Any) -> None:
    test_client, docker_client = client

    task = test_client.post(
        "/tasks", json={"project_name": "Sample Project"}
    ).json()

    # Accept refuses a task that is not in review, without running git.
    early = test_client.post(f"/tasks/{task['id']}/accept")
    assert early.status_code == 409
    assert "not in review" in early.json()["detail"]

    # Reject works straight from open, which is what frees the sandbox.
    docker_client.containers.settle_output = b"result deleted\nbase " + (
        BASE_COMMIT.encode()
    ) + b"\n"
    rejected = test_client.post(f"/tasks/{task['id']}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == TaskStatus.REJECTED.value

    # Idempotent: the same call returns the same settled task.
    again = test_client.post(f"/tasks/{task['id']}/reject")
    assert again.status_code == 200
    assert again.json()["settled_at"] == rejected.json()["settled_at"]

    assert test_client.post("/tasks/not-a-task-id/accept").status_code == 404


def test_accept_endpoint_reports_a_divergence_as_409(client: Any) -> None:
    test_client, docker_client = client

    task = test_client.post(
        "/tasks", json={"project_name": "Sample Project"}
    ).json()
    docker_client.containers.report_output = (
        f"head {NEXT_COMMIT}\nbranch {task['branch']}\n"
    ).encode()
    test_client.post(f"/tasks/{task['id']}/report", json={})
    for target in ("previewing", "review"):
        transition_task(
            get_controller_store(),
            task_id=task["id"],
            to_status=TaskStatus(target),
        )

    docker_client.containers.settle_output = (
        b"result diverged\nbase " + BASE_COMMIT.encode() + b"\ntask "
        + NEXT_COMMIT.encode() + b"\ncounts 1 4\n"
    )
    conflict = test_client.post(f"/tasks/{task['id']}/accept")

    assert conflict.status_code == 409
    assert "fast-forward merge is not possible" in conflict.json()["detail"]
    assert test_client.get(f"/tasks/{task['id']}").json()["status"] == (
        TaskStatus.REVIEW.value
    )
