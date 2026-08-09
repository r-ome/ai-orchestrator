from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.main import app
from app.turns.locators import TurnNotFound, locate


class StubDockerClient:
    """Stands in for the dependency; `running_containers` is patched to [].

    A real client is never touched: the tests below cover progress replay and
    scoping, not the attach socket, which `tests/previews` already exercises.
    """


@pytest.fixture(autouse=True)
def clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


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
    for session_id in ("session-1", "session-2"):
        store.create_planning_session(
            session_id=session_id,
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
    return store


def _configure(monkeypatch: pytest.MonkeyPatch, store: ControllerStore) -> None:
    app.dependency_overrides[get_docker_client] = lambda: StubDockerClient()
    app.dependency_overrides[get_controller_store] = lambda: store
    monkeypatch.setattr("app.turns.router.running_containers", lambda *_: [])


def _progress(store: ControllerStore, step: str, message: str) -> None:
    store.event(
        sandbox_id="sandbox-1",
        run_id="context-1",
        kind="context.progress",
        payload={"step": step, "message": message, "level": "info"},
    )


def test_the_socket_replays_progress_recorded_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _configure(monkeypatch, store)
    _progress(store, "claimed", "Context revision 1 reserved")
    _progress(store, "turn", "Running the claude context turn")

    client = TestClient(app)
    with client.websocket_connect(
        "/projects/sample/planning/sessions/session-1/turns/context/context-1/events"
    ) as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "progress"
    assert first["step"] == "claimed"
    assert second["step"] == "turn"
    assert second["message"] == "Running the claude context turn"


def test_a_terminal_step_ends_the_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this the page would keep a socket open on a finished turn."""
    store = _store(tmp_path)
    _configure(monkeypatch, store)
    _progress(store, "claimed", "Context revision 1 reserved")
    _progress(store, "settled", "Context revision 1 is ready")

    client = TestClient(app)
    with client.websocket_connect(
        "/projects/sample/planning/sessions/session-1/turns/context/context-1/events"
    ) as websocket:
        assert websocket.receive_json()["step"] == "claimed"
        assert websocket.receive_json()["step"] == "settled"
        assert websocket.receive_json() == {"type": "end"}


def test_only_this_session_progress_is_carried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events are read by run id and kind, so a neighbouring turn cannot leak."""
    store = _store(tmp_path)
    _configure(monkeypatch, store)
    store.event(
        sandbox_id="sandbox-1",
        run_id="some-other-run",
        kind="context.progress",
        payload={"step": "turn", "message": "Not this one", "level": "info"},
    )
    _progress(store, "settled", "Context revision 1 is ready")

    client = TestClient(app)
    with client.websocket_connect(
        "/projects/sample/planning/sessions/session-1/turns/context/context-1/events"
    ) as websocket:
        first = websocket.receive_json()
        assert first["message"] == "Context revision 1 is ready"
        assert websocket.receive_json() == {"type": "end"}


def test_a_context_from_another_session_is_not_reachable(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(TurnNotFound):
        locate(store, "context", "context-1", session_id="session-2")


def test_an_unknown_kind_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(TurnNotFound):
        locate(store, "not-a-phase", "context-1", session_id="session-1")


def test_the_context_locator_targets_the_planning_turn_container(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    locator = locate(store, "context", "context-1", session_id="session-1")

    assert locator.event_kind == "context.progress"
    assert locator.filters()["label"] == [
        "orchestrator.managed=true",
        "orchestrator.kind=planning",
        "orchestrator.planning.session-id=session-1",
        "orchestrator.planning.role=implementation_context",
    ]
