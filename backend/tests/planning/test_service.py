import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.planning.config import PlanningSettings
from app.planning.models import (
    CreatePlanningSessionRequest,
    PlanningMessageRequest,
    PlanningRole,
    PlanningStatus,
)
from app.planning.runner import PlanningTurnError, TurnResult
from app.planning import service
from app.planning.service import PlanningOperationError, TurnKind


PROJECT = SimpleNamespace(
    sandbox_id="sandbox-1",
    name="Sample Project",
    source_path="/projects/sample",
    volume_name="orchestrator-project-sample",
    created_at="2026-08-06T00:00:00Z",
    ready=True,
)


@pytest.fixture
def controller_store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_sandbox(
        sandbox_id=PROJECT.sandbox_id,
        project_id="project-1",
        project_name=PROJECT.name,
        source_path=PROJECT.source_path,
        volume_name=PROJECT.volume_name,
        status="ready",
        created_at=PROJECT.created_at,
    )
    return store


@pytest.fixture
def settings() -> PlanningSettings:
    return PlanningSettings(
        clarifier_provider=AgentProvider.CLAUDE,
        planner_provider=AgentProvider.CLAUDE,
        reviewer_provider=AgentProvider.CODEX,
        credential_profile="default",
        max_review_turns=3,
        turn_timeout_seconds=10,
        planning_memory="2g",
        claude_model="opus",
        codex_model="gpt-5.6-terra",
        codex_reasoning_effort="high",
    )


@pytest.fixture(autouse=True)
def no_background_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "schedule_turn", lambda *_: None)
    monkeypatch.setattr(
        service,
        "ensure_sandbox_registered",
        lambda *_: (PROJECT.sandbox_id, "project-1", PROJECT),
    )


def _create(store: ControllerStore, settings: PlanningSettings):
    return service.create_session(
        object(),
        store,
        settings,
        PROJECT.name,
        CreatePlanningSessionRequest(title="Add planning", request="Plan project sessions"),
    )


def test_create_stores_request_as_first_message_and_starts_clarifying(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)

    assert session.status is PlanningStatus.CLARIFYING
    messages = controller_store.planning_messages(session.id)
    assert [(message["sequence"], message["role"], message["text"]) for message in messages] == [
        (1, "user", "Plan project sessions")
    ]


def test_create_rejects_project_that_is_not_ready(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "ensure_sandbox_registered",
        lambda *_: (_ for _ in ()).throw(
            service.ProjectOperationError(409, "Project 'Sample Project' is not ready")
        ),
    )

    with pytest.raises(PlanningOperationError, match="not ready") as error:
        _create(controller_store, settings)

    assert error.value.status_code == 409


def test_second_message_while_a_turn_runs_and_terminal_message_are_rejected(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)
    assert controller_store.claim_planning_turn(session.id)

    with pytest.raises(PlanningOperationError, match="already running") as running:
        service.post_message(
            controller_store, settings, PROJECT.name, session.id, PlanningMessageRequest(text="More")
        )
    assert running.value.status_code == 409

    controller_store.release_planning_turn(session.id)
    controller_store.advance_planning_status(
        session_id=session.id,
        from_statuses=("clarifying",),
        to_status="failed",
        settled=True,
    )
    with pytest.raises(PlanningOperationError) as terminal:
        service.post_message(
            controller_store, settings, PROJECT.name, session.id, PlanningMessageRequest(text="More")
        )
    assert terminal.value.status_code == 409


def test_awaiting_confirmation_rejects_plain_message_and_correct_restarts_clarifying(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)
    controller_store.advance_planning_status(
        session_id=session.id,
        from_statuses=("clarifying",),
        to_status="awaiting_confirmation",
    )
    with pytest.raises(PlanningOperationError, match="confirm, correct, or proceed"):
        service.post_message(
            controller_store, settings, PROJECT.name, session.id, PlanningMessageRequest(text="Correction")
        )

    corrected = service.correct_understanding(
        controller_store, settings, PROJECT.name, session.id, PlanningMessageRequest(text="Correction")
    )
    assert corrected.status is PlanningStatus.CLARIFYING
    assert controller_store.planning_messages(session.id)[-1]["text"] == "Correction"


def test_ready_clarifier_response_stores_summary_and_awaits_confirmation(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)
    result = TurnResult(
        raw_output='{"message":"Summary","questions":[],"ready_to_summarize":true}',
        payload={
            "message": "Summary",
            "questions": [],
            "ready_to_summarize": True,
            "understanding_summary": "The agreed outcome",
        },
    )

    service._apply_clarifier_result(controller_store, controller_store.planning_session(session.id), result)  # type: ignore[arg-type]

    stored = controller_store.planning_session(session.id)
    assert stored["status"] == "awaiting_confirmation"
    assert stored["understanding_summary"] == "The agreed outcome"


def test_confirm_freezes_complete_brief_and_moves_to_planning(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)
    controller_store.append_planning_message(session_id=session.id, role="clarifier", text="What scope?")
    controller_store.append_planning_message(session_id=session.id, role="user", text="Only the API")
    controller_store.set_planning_understanding(session_id=session.id, summary="Add the API only")
    controller_store.advance_planning_status(
        session_id=session.id, from_statuses=("clarifying",), to_status="awaiting_confirmation"
    )

    confirmed = service.confirm_understanding(controller_store, settings, PROJECT.name, session.id)

    assert confirmed.status is PlanningStatus.PLANNING
    stored = controller_store.planning_session(session.id)
    assert stored["confirmed"] == 1
    assert "Add planning" in stored["feature_brief"]
    assert "Plan project sessions" in stored["feature_brief"]
    assert "Add the API only" in stored["feature_brief"]
    assert "What scope?" in stored["feature_brief"]
    assert "Only the API" in stored["feature_brief"]


def test_proceed_from_clarifying_freezes_an_unconfirmed_brief(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)

    proceeded = service.proceed_without_confirmation(controller_store, settings, PROJECT.name, session.id)

    assert proceeded.status is PlanningStatus.PLANNING
    stored = controller_store.planning_session(session.id)
    assert stored["confirmed"] == 0
    assert "not confirmed; the human proceeded anyway" in stored["feature_brief"]


def test_cancel_discards_a_late_turn_result_and_releases_the_turn(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _create(controller_store, settings)
    entered = threading.Event()
    release = threading.Event()

    def late_result(*_: Any) -> TurnResult:
        entered.set()
        assert release.wait(2)
        return TurnResult(
            raw_output="late raw output",
            payload={
                "message": "Late questions",
                "questions": ["Ignored?"],
                "ready_to_summarize": False,
                "understanding_summary": "",
            },
        )

    monkeypatch.setattr(service, "_run_clarifier_turn", late_result)

    async def run() -> None:
        task = asyncio.create_task(service._run_turn(controller_store, settings, session.id, TurnKind.CLARIFIER))
        while not entered.is_set():
            await asyncio.sleep(0)
        service.cancel_session(controller_store, PROJECT.name, session.id)
        release.set()
        await task

    asyncio.run(run())
    stored = controller_store.planning_session(session.id)
    assert stored["status"] == "cancelled"
    assert stored["turn_state"] == "idle"
    messages = controller_store.planning_messages(session.id)
    assert messages[-1]["role"] == PlanningRole.SYSTEM.value
    assert messages[-1]["text"] == "late raw output"
    assert all(message["text"] != "Late questions" for message in messages)


def test_turn_error_fails_session_records_reason_and_releases_turn(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _create(controller_store, settings)
    monkeypatch.setattr(
        service,
        "_run_clarifier_turn",
        lambda *_: (_ for _ in ()).throw(PlanningTurnError(422, "bad JSON", "raw failure")),
    )

    asyncio.run(service._run_turn(controller_store, settings, session.id, TurnKind.CLARIFIER))

    stored = controller_store.planning_session(session.id)
    assert stored["status"] == "failed"
    assert stored["turn_state"] == "idle"
    assert stored["failure_reason"] == "bad JSON"
    assert controller_store.planning_messages(session.id)[-1]["raw_output"] == "raw failure"
