from typing import Any

import pytest
from conftest import register_ready_v1_sandbox
from fastapi.testclient import TestClient

from app.controller.store import get_controller_store
from app.main import app
from app.planning import service
from app.planning.config import get_planning_settings
from app.platform.docker_client import get_docker_client
from app.projects.models import ProjectRegistration
from app.projects.service import managed_project_key, project_id

PROJECT_ID = project_id("/projects/sample")
PROJECT = ProjectRegistration(
    sandbox_id="sandbox-1",
    name="Sample Project",
    source_path=f"managed:{PROJECT_ID}",
    volume_name="orchestrator-project-sample",
    created_at="2026-08-06T00:00:00Z",
    ready=True,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    docker_client = object()
    monkeypatch.setattr(service, "schedule_turn", lambda *_: None)

    def ensure(_: object, store: Any, __: str):
        project_key = managed_project_key(PROJECT.source_path)
        register_ready_v1_sandbox(
            store,
            sandbox_id=PROJECT.sandbox_id,
            project_id=project_key,
            project_name=PROJECT.name,
            volume_name=PROJECT.volume_name,
            created_at=PROJECT.created_at,
        )
        return PROJECT.sandbox_id, project_key, PROJECT

    monkeypatch.setattr(
        service,
        "ensure_sandbox_registered",
        ensure,
    )
    monkeypatch.setattr(service, "inspect_registered_project", lambda *_: PROJECT)
    app.dependency_overrides[get_docker_client] = lambda: docker_client
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _create(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/projects/Sample%20Project/planning/sessions",
        json={"title": "Add planning", "request": "Plan this feature"},
    )
    assert response.status_code == 201
    return response.json()


def test_defaults_returns_planning_settings_and_provider_catalogues(
    client: TestClient,
) -> None:
    response = client.get("/projects/Sample%20Project/planning/defaults")

    assert response.status_code == 200
    body = response.json()
    settings = get_planning_settings()
    assert body["clarifier_provider"] == settings.clarifier_provider.value
    assert body["planner_provider"] == settings.planner_provider.value
    assert body["reviewer_provider"] == settings.reviewer_provider.value
    assert body["claude_model"] == settings.claude_model
    assert body["codex_model"] == settings.codex_model
    assert body["codex_reasoning_effort"] == settings.codex_reasoning_effort
    assert body["max_review_turns"] == settings.max_review_turns
    assert body["models_by_provider"]["claude"]
    assert body["models_by_provider"]["codex"]
    assert body["reasoning_efforts"] == ["low", "medium", "high"]


def test_planning_endpoints_return_the_specified_statuses_and_models(
    client: TestClient,
) -> None:
    session = _create(client)
    session_id = session["id"]
    assert session["status"] == "clarifying"

    listed = client.get("/projects/Sample%20Project/planning/sessions")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    detail = client.get(f"/projects/Sample%20Project/planning/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["text"] == "Plan this feature"

    message = client.post(
        f"/projects/Sample%20Project/planning/sessions/{session_id}/messages",
        json={"text": "Use the existing store"},
    )
    assert message.status_code == 202

    store = get_controller_store()
    store.advance_planning_status(
        session_id=session_id,
        from_statuses=("clarifying",),
        to_status="awaiting_confirmation",
    )
    corrected = client.post(
        f"/projects/Sample%20Project/planning/sessions/{session_id}/correct",
        json={"text": "Do not add dependencies"},
    )
    assert corrected.status_code == 202

    store.advance_planning_status(
        session_id=session_id,
        from_statuses=("clarifying",),
        to_status="awaiting_confirmation",
    )
    confirmed = client.post(
        f"/projects/Sample%20Project/planning/sessions/{session_id}/confirm",
    )
    assert confirmed.status_code == 202
    assert confirmed.json()["status"] == "planning"

    second = _create(client)
    proceeded = client.post(
        f"/projects/Sample%20Project/planning/sessions/{second['id']}/proceed",
    )
    assert proceeded.status_code == 202
    assert proceeded.json()["status"] == "planning"

    cancelled = client.post(
        f"/projects/Sample%20Project/planning/sessions/{second['id']}/cancel",
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_session_id_from_another_project_is_not_reachable(client: TestClient) -> None:
    session = _create(client)

    response = client.get(
        f"/projects/Other%20Project/planning/sessions/{session['id']}"
    )

    assert response.status_code == 404


def test_raw_output_endpoint_serves_one_turn_log(client: TestClient) -> None:
    session = _create(client)
    session_id = session["id"]
    store = get_controller_store()
    sequence = store.append_planning_message(
        session_id=session_id,
        role="clarifier",
        text="What is the desired outcome?",
        payload={"questions": ["What is the desired outcome?"]},
        raw_output='{"result": "…"}',
    )

    response = client.get(
        f"/projects/Sample%20Project/planning/sessions/{session_id}"
        f"/messages/{sequence}/raw"
    )

    assert response.status_code == 200
    assert response.json() == {
        "sequence": sequence,
        "role": "clarifier",
        "raw_output": '{"result": "…"}',
    }


def test_session_payload_flags_raw_output_without_carrying_it(
    client: TestClient,
) -> None:
    """The page polls the session every two seconds, so logs stay out of it."""
    session = _create(client)
    session_id = session["id"]
    store = get_controller_store()
    store.append_planning_message(
        session_id=session_id,
        role="clarifier",
        text="What is the desired outcome?",
        raw_output="x" * 5000,
    )

    body = client.get(
        f"/projects/Sample%20Project/planning/sessions/{session_id}"
    ).json()

    # The opening request carries no log; the clarifier turn has one to fetch.
    assert [message["has_raw_output"] for message in body["messages"]] == [False, True]
    assert "raw_output" not in body["messages"][1]


def test_raw_output_of_an_unknown_sequence_is_not_found(client: TestClient) -> None:
    session = _create(client)

    response = client.get(
        f"/projects/Sample%20Project/planning/sessions/{session['id']}/messages/99/raw"
    )

    assert response.status_code == 404


def test_raw_output_is_not_reachable_through_another_project(
    client: TestClient,
) -> None:
    session = _create(client)

    response = client.get(
        f"/projects/Other%20Project/planning/sessions/{session['id']}/messages/1/raw"
    )

    assert response.status_code == 404
