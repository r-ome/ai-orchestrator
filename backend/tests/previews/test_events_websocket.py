import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.main import app
from app.previews.service import _record_preview_progress
from app.projects.service import managed_project_key

client = TestClient(app)


class StubDockerClient:
    """Enough surface for `preview_events` when no container ever appears.

    `require_preview_proposal` only needs `inspect_registered_project`, which
    this test monkeypatches directly, so the docker client itself is never
    called — its presence just satisfies the dependency's type.
    """


@pytest.fixture(autouse=True)
def clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _ready_project(sandbox_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        name="events-sandbox-1",
        sandbox_id=sandbox_id,
        source_path="/projects/events-sandbox-1",
        volume_name="events-sandbox-1-volume",
        ready=True,
        created_at="2026-08-06T00:00:00Z",
    )


def _configure(monkeypatch: pytest.MonkeyPatch, *, sandbox_id: str) -> ControllerStore:
    app.dependency_overrides[get_docker_client] = lambda: StubDockerClient()
    project = _ready_project(sandbox_id)
    monkeypatch.setattr(
        "app.previews.service.inspect_registered_project",
        lambda *_: project,
    )
    monkeypatch.setattr(
        "app.previews.router.preview_running_containers",
        lambda *_: [],
    )
    store = get_controller_store()
    # `_approve` below needs the sandbox row to exist, because
    # review_rounds.sandbox_id is a NOT NULL foreign key. Derive the project id
    # the way the service does, so both inserts agree.
    store.register_sandbox(
        sandbox_id=sandbox_id,
        project_id=managed_project_key(project.source_path),
        project_name=project.name,
        source_path=project.source_path,
        volume_name=project.volume_name,
        status="ready",
        created_at=project.created_at,
    )
    return store


def _approve(store: ControllerStore, *, sandbox_id: str, proposal_id: str) -> None:
    store.create_review(
        review_id=proposal_id,
        sandbox_id=sandbox_id,
        proposal_digest="digest",
        detected_mode="native",
        config={},
        protected_files={},
        changes=[],
        created_at="2026-08-06T00:00:00Z",
        expires_at="2099-01-01T00:00:00Z",
    )


def test_events_websocket_replays_events_recorded_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_id = "sandbox-replay"
    proposal_id = "proposal-replay"
    store = _configure(monkeypatch, sandbox_id=sandbox_id)
    _approve(store, sandbox_id=sandbox_id, proposal_id=proposal_id)

    _record_preview_progress(
        store,
        sandbox_id=sandbox_id,
        proposal_id=proposal_id,
        preview_id="preview-replay",
        status="preparing",
        step="approved",
        message="Approved native preview settings",
    )
    _record_preview_progress(
        store,
        sandbox_id=sandbox_id,
        proposal_id=proposal_id,
        preview_id="preview-replay",
        status="running",
        step="ready",
        message="Preview is running",
        duration_ms=123,
        started_at="2026-08-06T00:00:00Z",
    )

    with client.websocket_connect(
        f"/projects/events-sandbox-1/preview-proposals/{proposal_id}/events"
    ) as websocket:
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first["type"] == "progress"
    assert first["step"] == "approved"
    assert first["duration_ms"] is None
    assert second["step"] == "ready"
    assert second["status"] == "running"
    assert second["duration_ms"] == 123
    assert second["started_at"] == "2026-08-06T00:00:00Z"


def test_events_websocket_streams_events_added_after_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_id = "sandbox-live"
    proposal_id = "proposal-live"
    store = _configure(monkeypatch, sandbox_id=sandbox_id)
    _approve(store, sandbox_id=sandbox_id, proposal_id=proposal_id)

    _record_preview_progress(
        store,
        sandbox_id=sandbox_id,
        proposal_id=proposal_id,
        preview_id="preview-live",
        status="preparing",
        step="approved",
        message="Approved native preview settings",
    )

    def publish_completion() -> None:
        time.sleep(0.2)
        _record_preview_progress(
            store,
            sandbox_id=sandbox_id,
            proposal_id=proposal_id,
            preview_id="preview-live",
            status="running",
            step="ready",
            message="Preview is running",
            duration_ms=456,
        )

    publisher = threading.Thread(target=publish_completion)
    publisher.start()
    try:
        with client.websocket_connect(
            f"/projects/events-sandbox-1/preview-proposals/{proposal_id}/events"
        ) as websocket:
            first = websocket.receive_json()
            second = websocket.receive_json()
    finally:
        publisher.join(timeout=5)

    assert first["step"] == "approved"
    assert second["step"] == "ready"
    assert second["duration_ms"] == 456


def test_events_websocket_disconnect_clears_the_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_id = "sandbox-disconnect"
    proposal_id = "proposal-disconnect"
    store = _configure(monkeypatch, sandbox_id=sandbox_id)
    _approve(store, sandbox_id=sandbox_id, proposal_id=proposal_id)

    _record_preview_progress(
        store,
        sandbox_id=sandbox_id,
        proposal_id=proposal_id,
        preview_id="",
        status="preparing",
        step="approved",
        message="Approved native preview settings",
    )

    from app.previews.router import _active_event_sessions

    with client.websocket_connect(
        f"/projects/events-sandbox-1/preview-proposals/{proposal_id}/events"
    ) as websocket:
        websocket.receive_json()
        assert f"events-sandbox-1:{proposal_id}" in _active_event_sessions

    assert f"events-sandbox-1:{proposal_id}" not in _active_event_sessions


def test_events_websocket_closes_when_proposal_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, sandbox_id="sandbox-missing")

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/projects/events-sandbox-1/preview-proposals/does-not-exist/events"
        ):
            pass

    assert excinfo.value.code == 4404
