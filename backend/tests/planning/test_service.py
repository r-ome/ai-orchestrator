import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import register_ready_v1_sandbox

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
from app.projects.models import ProjectRegistration


PROJECT = ProjectRegistration(
    sandbox_id="sandbox-1",
    name="Sample Project",
    source_path="managed:project-1",
    volume_name="orchestrator-project-sample",
    created_at="2026-08-06T00:00:00Z",
    ready=True,
)


@pytest.fixture
def controller_store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id=PROJECT.sandbox_id,
        project_id="project-1",
        project_name=PROJECT.name,
        volume_name=PROJECT.volume_name,
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


def test_illegal_planning_transition_changes_nothing(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)

    assert not service.transition_planning_session(
        controller_store,
        session_id=session.id,
        to_status=PlanningStatus.UNDER_REVIEW,
    )
    assert controller_store.planning_session(session.id)["status"] == PlanningStatus.CLARIFYING.value


def test_create_records_resolved_model_defaults(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = _create(controller_store, settings)

    stored = controller_store.planning_session(session.id)
    assert stored is not None
    assert stored["clarifier_model"] == "opus"
    assert stored["planner_model"] == "opus"
    assert stored["reviewer_model"] == "gpt-5.6-terra"
    assert stored["reviewer_reasoning_effort"] == "high"


def test_create_rejects_a_model_owned_by_another_provider(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    with pytest.raises(PlanningOperationError, match="claude model and codex cannot run it") as error:
        service.create_session(
            object(),
            controller_store,
            settings,
            PROJECT.name,
            CreatePlanningSessionRequest(
                title="Add planning",
                request="Plan project sessions",
                clarifier_provider=AgentProvider.CODEX,
                clarifier_model="claude-fable-5",
            ),
        )

    assert error.value.status_code == 422


def test_create_accepts_an_unlisted_model_and_rejects_bad_reasoning_effort(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    session = service.create_session(
        object(),
        controller_store,
        settings,
        PROJECT.name,
        CreatePlanningSessionRequest(
            title="Add planning",
            request="Plan project sessions",
            clarifier_model="claude-next",
        ),
    )
    stored = controller_store.planning_session(session.id)
    assert stored is not None
    assert stored["clarifier_model"] == "claude-next"

    with pytest.raises(PlanningOperationError, match="reasoning effort") as error:
        service.create_session(
            object(),
            controller_store,
            settings,
            PROJECT.name,
            CreatePlanningSessionRequest(
                title="Bad effort",
                request="Plan project sessions",
                reviewer_reasoning_effort="maximum",
            ),
        )

    assert error.value.status_code == 422


def test_a_configured_effort_outside_the_dialog_choices_is_not_refused(
    controller_store: ControllerStore, settings: PlanningSettings
) -> None:
    """The dialog offers three efforts. A deployment may configure another."""
    configured = replace(settings, codex_reasoning_effort="minimal")

    session = _create(controller_store, configured)

    stored = controller_store.planning_session(session.id)
    assert stored is not None
    assert stored["reviewer_reasoning_effort"] == "minimal"

    # The dialog offers the configured effort, so sending it back must work.
    echoed = service.create_session(
        object(),
        controller_store,
        configured,
        PROJECT.name,
        CreatePlanningSessionRequest(
            title="Echo the effort",
            request="Plan project sessions",
            reviewer_reasoning_effort="minimal",
        ),
    )
    assert controller_store.planning_session(echoed.id)["reviewer_reasoning_effort"] == "minimal"


def test_each_role_runs_with_its_stored_model(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = service.create_session(
        object(),
        controller_store,
        settings,
        PROJECT.name,
        CreatePlanningSessionRequest(
            title="Add planning",
            request="Plan project sessions",
            clarifier_model="claude-fable-5",
            planner_provider=AgentProvider.CODEX,
            planner_model="gpt-5.6-sol",
            reviewer_model="gpt-5.6-terra",
            reviewer_reasoning_effort="low",
        ),
    )
    controller_store.record_plan_revision(
        session_id=session.id,
        revision=1,
        plan_json={},
        plan_markdown="# Plan",
    )
    controller_store.advance_planning_status(
        session_id=session.id,
        from_statuses=(PlanningStatus.CLARIFYING.value,),
        to_status=PlanningStatus.PLANNING.value,
    )
    stored = controller_store.planning_session(session.id)
    assert stored is not None
    assert stored["clarifier_model"] == "claude-fable-5"
    assert stored["planner_model"] == "gpt-5.6-sol"
    assert stored["reviewer_model"] == "gpt-5.6-terra"
    assert stored["reviewer_reasoning_effort"] == "low"
    captured: list[tuple[PlanningSettings, AgentProvider]] = []

    def run(_: object, turn_settings: PlanningSettings, request: Any) -> TurnResult:
        captured.append((turn_settings, request.provider))
        payload = {
            PlanningRole.CLARIFIER: {
                "message": "Ready.",
                "questions": [],
                "ready_to_summarize": True,
                "understanding_summary": "Complete.",
            },
            PlanningRole.PLANNER: {
                "plan_markdown": "# Plan",
                "scope": "Scope",
                "approach": "Approach",
                "components": [],
                "risks": [],
                "open_questions": [],
            },
            PlanningRole.REVIEWER: {
                "approved": True,
                "summary": "Approved.",
                "findings": [],
            },
        }[request.role]
        return TurnResult(raw_output="", payload=payload)

    monkeypatch.setattr(service, "run_planning_turn", run)
    monkeypatch.setattr(service, "_turn_client", lambda: SimpleNamespace(close=lambda: None))

    service._run_clarifier_turn(controller_store, settings, session.id)
    service._run_planner_turn(controller_store, settings, session.id)
    service._run_reviewer_turn(controller_store, settings, session.id)

    assert [(turn.claude_model, turn.codex_model, turn.codex_reasoning_effort, provider) for turn, provider in captured] == [
        ("claude-fable-5", "gpt-5.6-terra", "high", AgentProvider.CLAUDE),
        ("opus", "gpt-5.6-sol", "high", AgentProvider.CODEX),
        ("opus", "gpt-5.6-terra", "low", AgentProvider.CODEX),
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


def test_transient_turn_failure_classifier_matches_capacity_and_rate_limit() -> None:
    assert service._is_transient_turn_failure(
        PlanningTurnError(
            502,
            "reviewer turn exited with status 1: ERROR: Selected model is at capacity. "
            "Please try a different model.",
        )
    )
    assert service._is_transient_turn_failure(
        PlanningTurnError(502, "reviewer failed", "Provider rate_limit reached")
    )


def test_transient_classifier_ignores_bare_status_numbers_and_timeouts() -> None:
    """Guards two rules, neither of which is the general false-positive case.

    The plan-text case belongs to
    test_transient_classifier_ignores_a_reviewed_plan_that_discusses_rate_limits.
    """
    # Bare numbers must never join the phrase list. A plan naming a status code
    # is ordinary, and matching one would retry genuine failures.
    assert not service._is_transient_turn_failure(
        PlanningTurnError(
            502,
            "reviewer turn exited with status 1",
            "# Plan\nReturn a 503 response when the resource is absent.",
        )
    )
    # "Service unavailable" is in the phrase list, so this pins the 504 check
    # ahead of phrase matching: a timeout stays terminal whatever it printed.
    assert not service._is_transient_turn_failure(
        PlanningTurnError(504, "reviewer turn timed out", "Service unavailable")
    )


def test_transient_classifier_ignores_a_reviewed_plan_that_discusses_rate_limits() -> None:
    """The echoed prompt embeds the plan, which may legitimately use these words."""
    reviewed_plan = "\n".join(
        [
            "# Plan",
            "Add a token bucket so the API returns 429 when the rate limit is exceeded.",
            "Show a banner while the service is temporarily unavailable.",
        ]
        + [f"Step {number}: implement the handler." for number in range(20)]
    )

    assert not service._is_transient_turn_failure(
        PlanningTurnError(502, "reviewer turn exited with status 1", reviewed_plan)
    )
    # The same output with a real provider error appended is still transient.
    assert service._is_transient_turn_failure(
        PlanningTurnError(
            502,
            "reviewer turn exited with status 1",
            f"{reviewed_plan}\nERROR: Selected model is at capacity.",
        )
    )


def test_transient_reviewer_failure_retries_without_losing_the_current_revision(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _create(controller_store, settings)
    controller_store.set_planning_understanding(session_id=session.id, summary="Build the feature")
    controller_store.advance_planning_status(
        session_id=session.id,
        from_statuses=(PlanningStatus.CLARIFYING.value,),
        to_status=PlanningStatus.AWAITING_CONFIRMATION.value,
    )
    service.confirm_understanding(controller_store, settings, PROJECT.name, session.id)
    plan = {
        "plan_markdown": "# Existing plan",
        "scope": "Implement the feature.",
        "approach": "Use the current service.",
        "components": [],
        "risks": [],
        "open_questions": [],
    }
    controller_store.record_plan_revision(
        session_id=session.id,
        revision=1,
        plan_json=plan,
        plan_markdown=plan["plan_markdown"],
    )
    controller_store.advance_planning_status(
        session_id=session.id,
        from_statuses=(PlanningStatus.PLANNING.value,),
        to_status=PlanningStatus.UNDER_REVIEW.value,
    )
    attempts = 0
    delays: list[int] = []

    def run_reviewer(*_: Any) -> TurnResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PlanningTurnError(502, "Selected model is at capacity", "capacity")
        return TurnResult(
            raw_output='{"approved":true}',
            payload={"approved": True, "summary": "Approved.", "findings": []},
        )

    async def sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(service, "_run_reviewer_turn", run_reviewer)
    monkeypatch.setattr(service.asyncio, "sleep", sleep)

    asyncio.run(
        service._run_turn(
            controller_store,
            replace(settings, turn_retries=2, turn_retry_backoff_seconds=5),
            session.id,
            TurnKind.REVIEWER,
        )
    )

    stored = controller_store.planning_session(session.id)
    assert attempts == 2
    assert delays == [5]
    assert stored["status"] == PlanningStatus.PLAN_READY.value
    assert stored["plan_revision"] == 1
    assert stored["failure_reason"] == ""


def test_exhausted_transient_retries_fail_with_attempt_count(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _create(controller_store, settings)
    attempts = 0
    delays: list[int] = []

    def fail(*_: Any) -> TurnResult:
        nonlocal attempts
        attempts += 1
        raise PlanningTurnError(502, "Provider is overloaded", "overloaded")

    async def no_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(service, "_run_clarifier_turn", fail)
    monkeypatch.setattr(service.asyncio, "sleep", no_sleep)

    asyncio.run(
        service._run_turn(
            controller_store,
            replace(settings, turn_retries=2, turn_retry_backoff_seconds=5),
            session.id,
            TurnKind.CLARIFIER,
        )
    )

    stored = controller_store.planning_session(session.id)
    assert attempts == 3
    assert delays == [5, 10]
    assert stored["status"] == PlanningStatus.FAILED.value
    assert stored["failure_reason"] == "Provider is overloaded (after 3 attempts)"


def test_cancelled_session_between_retries_stays_cancelled(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _create(controller_store, settings)
    attempts = 0

    def fail(*_: Any) -> TurnResult:
        nonlocal attempts
        attempts += 1
        raise PlanningTurnError(502, "Selected model is at capacity", "capacity")

    async def cancel_before_retry(_: int) -> None:
        service.cancel_session(controller_store, PROJECT.name, session.id)

    monkeypatch.setattr(service, "_run_clarifier_turn", fail)
    monkeypatch.setattr(service.asyncio, "sleep", cancel_before_retry)

    asyncio.run(service._run_turn(controller_store, settings, session.id, TurnKind.CLARIFIER))

    stored = controller_store.planning_session(session.id)
    assert attempts == 1
    assert stored["status"] == PlanningStatus.CANCELLED.value
    assert stored["failure_reason"] == ""


def test_timeout_fails_without_retrying(
    controller_store: ControllerStore, settings: PlanningSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _create(controller_store, settings)
    attempts = 0

    def timeout(*_: Any) -> TurnResult:
        nonlocal attempts
        attempts += 1
        raise PlanningTurnError(504, "clarifier turn timed out after 600 seconds")

    monkeypatch.setattr(service, "_run_clarifier_turn", timeout)

    asyncio.run(service._run_turn(controller_store, settings, session.id, TurnKind.CLARIFIER))

    stored = controller_store.planning_session(session.id)
    assert attempts == 1
    assert stored["status"] == PlanningStatus.FAILED.value
    assert stored["failure_reason"] == "clarifier turn timed out after 600 seconds"
