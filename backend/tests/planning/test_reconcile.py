from pathlib import Path

import pytest

from conftest import register_ready_v1_sandbox
from docker.errors import DockerException

from app.controller.lifecycle import reconcile_controller_state
from app.controller.store import ControllerStore


def test_reconcile_fails_and_releases_running_turn_when_docker_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="Sample Project",
        volume_name="orchestrator-project-sample",
        created_at="2026-08-06T00:00:00Z",
    )
    store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="Sample Project",
        title="Plan",
        status="clarifying",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    assert store.claim_planning_turn("session-1")
    monkeypatch.setattr(
        "app.controller.lifecycle.docker.from_env",
        lambda: (_ for _ in ()).throw(DockerException("daemon unavailable")),
    )

    counts = reconcile_controller_state(store)

    session = store.planning_session("session-1")
    assert counts["planning"] == 1
    assert session["status"] == "failed"
    assert session["turn_state"] == "idle"
    assert session["failure_reason"] == "The backend restarted while this turn was running"
