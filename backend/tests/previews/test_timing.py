import time
from pathlib import Path

from conftest import register_ready_v1_sandbox
from app.controller.store import ControllerStore
from app.previews.progress import _ignore_progress, _record_preview_progress, _timed_step
from app.previews.service import (
    _preview_log_response,
)


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="timing-sandbox-1",
        volume_name="timing-volume",
        created_at="2026-08-06T00:00:00Z",
    )
    return store


def test_timed_step_emits_a_start_event_then_a_completion_event_with_duration(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    calls: list[tuple[str, str, int | None, str | None]] = []

    def report(
        step: str,
        message: str,
        duration_ms: int | None = None,
        started_at: str | None = None,
    ) -> None:
        calls.append((step, message, duration_ms, started_at))
        _record_preview_progress(
            store,
            sandbox_id="sandbox-1",
            proposal_id="proposal-1",
            preview_id="preview-1",
            status="preparing",
            step=step,
            message=message,
            duration_ms=duration_ms,
            started_at=started_at,
        )

    with _timed_step(report, "workspace", "Exporting sandbox commit abc1234") as finish:
        time.sleep(0.01)
        finish("Runtime workspace holds the task commit")

    assert len(calls) == 2
    start_step, start_message, start_duration, start_started_at = calls[0]
    assert start_step == "workspace"
    assert start_message == "Exporting sandbox commit abc1234"
    assert start_duration is None
    assert start_started_at is not None

    done_step, done_message, done_duration, done_started_at = calls[1]
    assert done_step == "workspace"
    assert done_message == "Runtime workspace holds the task commit"
    assert done_duration is not None
    assert done_duration >= 0
    assert done_started_at == start_started_at

    events = store.events_for_run("proposal-1", kind="preview.progress")
    assert [event["payload"]["message"] for event in events] == [
        "Exporting sandbox commit abc1234",
        "Runtime workspace holds the task commit",
    ]
    assert "duration_ms" not in events[0]["payload"]
    assert events[1]["payload"]["duration_ms"] == done_duration
    assert events[1]["payload"]["started_at"] == start_started_at


def test_timed_step_emits_no_completion_event_when_the_block_fails(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    def report(
        step: str,
        message: str,
        duration_ms: int | None = None,
        started_at: str | None = None,
    ) -> None:
        _record_preview_progress(
            store,
            sandbox_id="sandbox-1",
            proposal_id="proposal-2",
            preview_id="preview-2",
            status="preparing",
            step=step,
            message=message,
            duration_ms=duration_ms,
            started_at=started_at,
        )

    try:
        with _timed_step(report, "dependencies", "Running the install command") as finish:
            raise RuntimeError("npm exploded")
    except RuntimeError:
        pass

    events = store.events_for_run("proposal-2", kind="preview.progress")
    assert len(events) == 1
    assert events[0]["payload"]["message"] == "Running the install command"
    assert "duration_ms" not in events[0]["payload"]


def test_ignore_progress_accepts_started_at_and_duration_ms() -> None:
    # _timed_step calls report(step, message, duration_ms=..., started_at=...);
    # _ignore_progress is the default reporter and must accept that shape.
    _ignore_progress("workspace", "message", duration_ms=12, started_at="2026-08-06T00:00:00Z")


def test_reused_dependency_volume_still_reports_zero_duration_and_no_started_at(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    _record_preview_progress(
        store,
        sandbox_id="sandbox-1",
        proposal_id="proposal-3",
        preview_id="preview-3",
        status="preparing",
        step="dependencies",
        message="Dependency volume already installed for this lockfile; skipping install",
        duration_ms=0,
    )

    events = store.events_for_run("proposal-3", kind="preview.progress")
    assert events[0]["payload"]["duration_ms"] == 0
    assert "started_at" not in events[0]["payload"]


def test_preview_log_response_surfaces_duration_and_started_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_preview_progress(
        store,
        sandbox_id="sandbox-1",
        proposal_id="proposal-4",
        preview_id="preview-4",
        status="preparing",
        step="container",
        message="Creating application container",
        started_at="2026-08-06T00:00:00Z",
    )
    _record_preview_progress(
        store,
        sandbox_id="sandbox-1",
        proposal_id="proposal-4",
        preview_id="preview-4",
        status="running",
        step="container",
        message="Application container started",
        duration_ms=842,
        started_at="2026-08-06T00:00:00Z",
    )

    # preview_id is empty, so the container-log lookup is never reached and no
    # Docker client call happens.
    logs = _preview_log_response(
        None,
        store,
        proposal_id="proposal-4",
        preview_id="",
        fallback_status="preparing",
    )

    assert logs.events[0].duration_ms is None
    assert logs.events[0].started_at == "2026-08-06T00:00:00Z"
    assert logs.events[1].duration_ms == 842
    assert logs.events[1].started_at == "2026-08-06T00:00:00Z"
