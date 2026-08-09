from pathlib import Path

from fastapi.testclient import TestClient

from app.controller.store import ControllerStore, get_controller_store
from app.main import app


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        source_path="/projects/sample",
        volume_name="sample-volume",
        status="ready",
        created_at="2026-08-08T00:00:00Z",
    )
    store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Sample plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "test-model",
        }
    )
    store.settle_implementation_context("context-1", to_status="ready")
    return store


def test_context_routes_are_nested_under_the_planning_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    app.dependency_overrides[get_controller_store] = lambda: store
    client = TestClient(app)
    path = "/projects/sample/planning/sessions/session-1/implementation-context"

    try:
        response = client.get(path)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == "context-1"


def test_context_routes_enforce_project_and_session_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    app.dependency_overrides[get_controller_store] = lambda: store
    client = TestClient(app)

    try:
        wrong_project = client.get(
            "/projects/other/planning/sessions/session-1/implementation-context"
        )
        wrong_session = client.get(
            "/projects/sample/planning/sessions/other/implementation-context"
        )
    finally:
        app.dependency_overrides.clear()

    assert wrong_project.status_code == 404
    assert wrong_session.status_code == 404
