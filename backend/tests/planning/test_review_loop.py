import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import register_ready_v1_sandbox

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.planning import service
from app.planning.config import PlanningSettings
from app.planning.models import (
    CreatePlanningSessionRequest,
    PlanSpec,
    PlanningRole,
    PlanningStatus,
)
from app.planning.runner import TurnRequest, TurnResult
from app.planning.service import TurnKind
from app.projects.models import ProjectRegistration


TEST_MODEL = "test-model"

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


@pytest.fixture
def scripted_runner(monkeypatch: pytest.MonkeyPatch) -> tuple[deque[dict[str, Any]], list[str]]:
    queue: deque[dict[str, Any]] = deque()
    prompts: list[str] = []

    def run(request: TurnRequest) -> TurnResult:
        # Every turn reads the sandbox's own project volume. A different volume
        # would mean the turn planned against another sandbox's code.
        assert request.project_volume == PROJECT.volume_name
        prompts.append(request.prompt)
        payload = queue.popleft()
        return TurnResult(raw_output=str(payload), payload=payload, model=TEST_MODEL)

    monkeypatch.setattr(service, "schedule_turn", lambda *_: None)
    monkeypatch.setattr(
        service,
        "run_planning_turn",
        lambda _client, _settings, request: run(request),
    )
    monkeypatch.setattr(service, "_turn_client", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        service,
        "ensure_sandbox_registered",
        lambda *_: (PROJECT.sandbox_id, "project-1", PROJECT),
    )
    return queue, prompts


def _plan(*, responses: list[dict[str, str]] | None = None, name: str = "First") -> dict[str, Any]:
    return {
        "plan_markdown": f"# {name} plan\nImplement the planning flow.",
        "scope": "Add the planning flow and exclude execution.",
        "approach": "Store revisions and review each revision independently.",
        "components": [{"name": "planning service", "responsibility": "run the loop"}],
        "risks": [{"severity": "medium", "text": "A reviewer can reject a valid plan."}],
        "open_questions": [],
        "finding_responses": responses or [],
    }


def _review(
    *, approved: bool, findings: list[dict[str, str]], summary: str = "Review complete."
) -> dict[str, Any]:
    return {"approved": approved, "summary": summary, "findings": findings}


def _start(
    store: ControllerStore,
    settings: PlanningSettings,
) -> str:
    session = service.create_session(
        object(),
        store,
        settings,
        PROJECT.name,
        CreatePlanningSessionRequest(title="Planning flow", request="Add planning sessions"),
    )
    store.set_planning_understanding(session_id=session.id, summary="Build a reviewed planning flow")
    store.advance_planning_status(
        session_id=session.id,
        from_statuses=(PlanningStatus.CLARIFYING.value,),
        to_status=PlanningStatus.AWAITING_CONFIRMATION.value,
    )
    service.confirm_understanding(store, settings, PROJECT.name, session.id)
    return session.id


def _run(store: ControllerStore, settings: PlanningSettings, session_id: str, kind: TurnKind) -> None:
    asyncio.run(service._run_turn(store, settings, session_id, kind))


def test_round_one_approval_writes_an_approved_plan_spec(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend([_plan(), _review(approved=True, findings=[])])
    session_id = _start(controller_store, settings)

    _run(controller_store, settings, session_id, TurnKind.PLANNER)
    _run(controller_store, settings, session_id, TurnKind.REVIEWER)

    session = controller_store.planning_session(session_id)
    assert session is not None
    assert session["status"] == PlanningStatus.PLAN_READY.value
    assert session["plan_revision"] == 1
    assert session["review_turn"] == 1
    spec = service._json_value(session["plan_spec_json"])
    assert spec["reviewer_outcome"]["approved"] is True
    assert spec["reviewer_outcome"]["outstanding_findings"] == []


def test_rejection_then_approval_records_two_review_rounds(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "major", "text": "Add recovery."}]),
            _plan(responses=[{"finding_id": "F1", "status": "answered", "rationale": "Added recovery."}], name="Second"),
            _review(approved=True, findings=[]),
        ]
    )
    session_id = _start(controller_store, settings)

    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER, TurnKind.REVIEWER):
        _run(controller_store, settings, session_id, kind)

    session = controller_store.planning_session(session_id)
    assert session is not None
    assert session["status"] == PlanningStatus.PLAN_READY.value
    assert service._json_value(session["plan_spec_json"])["reviewer_outcome"]["rounds"] == 2


def test_three_rejections_reach_the_limit_with_outstanding_findings(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "major", "text": "Add recovery."}]),
            _plan(name="Second"),
            _review(approved=False, findings=[{"id": "F1", "severity": "major", "text": "Add recovery."}]),
            _plan(name="Third"),
            _review(approved=False, findings=[{"id": "F1", "severity": "major", "text": "Add recovery."}]),
        ]
    )
    session_id = _start(controller_store, settings)

    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER) * 3:
        _run(controller_store, settings, session_id, kind)

    session = controller_store.planning_session(session_id)
    assert session is not None
    assert session["status"] == PlanningStatus.REVIEW_LIMIT_REACHED.value
    spec = service._json_value(session["plan_spec_json"])
    assert spec["reviewer_outcome"]["approved"] is False
    assert [finding["finding_id"] for finding in spec["reviewer_outcome"]["outstanding_findings"]] == ["F1"]


def test_second_round_prompts_keep_only_the_required_context(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, requests = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "major", "text": "Add recovery."}]),
            _plan(responses=[{"finding_id": "F1", "status": "answered", "rationale": "Added recovery."}], name="Second"),
            _review(approved=True, findings=[]),
        ]
    )
    session_id = _start(controller_store, settings)
    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER, TurnKind.REVIEWER):
        _run(controller_store, settings, session_id, kind)

    planner_two = requests[2]
    reviewer_two = requests[3]
    assert "# First plan" in planner_two
    assert "F1" in planner_two
    assert "Add recovery." in planner_two
    assert "F1" in reviewer_two
    assert "Add recovery." in reviewer_two
    assert "Your previous planning turns" not in reviewer_two
    assert "# First plan" not in reviewer_two


def test_reraised_finding_keeps_its_stable_id_and_original_round(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "minor", "text": "Name the owner."}]),
            _plan(name="Second"),
            _review(approved=False, findings=[{"id": "F1", "severity": "minor", "text": "Name the owner."}]),
        ]
    )
    session_id = _start(controller_store, settings)
    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER, TurnKind.REVIEWER):
        _run(controller_store, settings, session_id, kind)

    finding = controller_store.planning_findings(session_id)[0]
    assert finding["finding_id"] == "F1"
    assert finding["raised_in_round"] == 1
    assert finding["last_seen_round"] == 2


def test_unseen_finding_becomes_resolved_and_leaves_the_next_ledger(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, requests = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "minor", "text": "Name the owner."}]),
            _plan(name="Second"),
            _review(approved=False, findings=[]),
            _plan(name="Third"),
        ]
    )
    session_id = _start(controller_store, settings)
    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER):
        _run(controller_store, settings, session_id, kind)

    assert controller_store.planning_findings(session_id)[0]["status"] == "resolved"
    assert "Name the owner." not in requests[-1]


def test_new_ids_are_minted_then_reused_from_the_ledger(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "minor", "text": "Name the owner."}]),
            _plan(name="Second"),
            _review(approved=False, findings=[{"id": "F1", "severity": "minor", "text": "Name the owner."}]),
        ]
    )
    session_id = _start(controller_store, settings)
    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER, TurnKind.REVIEWER):
        _run(controller_store, settings, session_id, kind)

    findings = controller_store.planning_findings(session_id)
    assert [finding["finding_id"] for finding in findings] == ["F1"]


def test_approved_verdict_with_blocking_finding_is_overridden(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=True, findings=[{"id": "NEW-1", "severity": "blocking", "text": "Plan lacks safety."}]),
        ]
    )
    session_id = _start(controller_store, settings)
    _run(controller_store, settings, session_id, TurnKind.PLANNER)
    _run(controller_store, settings, session_id, TurnKind.REVIEWER)

    session = controller_store.planning_session(session_id)
    assert session is not None
    assert session["status"] == PlanningStatus.PLANNING.value
    assert any(
        message["role"] == "system" and "verdict was overridden" in message["text"]
        for message in controller_store.planning_messages(session_id)
    )


def test_missing_planner_response_leaves_the_finding_open(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "minor", "text": "Name the owner."}]),
            _plan(name="Second"),
        ]
    )
    session_id = _start(controller_store, settings)
    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER):
        _run(controller_store, settings, session_id, kind)

    assert controller_store.planning_findings(session_id)[0]["status"] == "open"


def test_planner_responses_to_unknown_findings_are_rejected_and_repaired(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    """Round one has no ledger, so any finding response is invented.

    Rejecting the turn rather than dropping the response matters: a planner that
    answers a finding nobody raised has misread its task, and it wrote the plan
    against that misreading. The repair loop makes it plan again.
    """
    queue, requests = scripted_runner
    queue.extend(
        [
            _plan(
                responses=[
                    {"finding_id": "F1", "status": "answered", "rationale": "Invented."},
                    {"finding_id": "F2", "status": "rejected", "rationale": "Invented."},
                ]
            ),
            _plan(name="Repaired"),
        ]
    )
    session_id = _start(controller_store, settings)
    _run(controller_store, settings, session_id, TurnKind.PLANNER)

    messages = [
        service._message_model(row)
        for row in controller_store.planning_messages(session_id)
    ]
    plans = [message for message in messages if message.role == PlanningRole.PLANNER]
    # Only the repaired turn is stored. The rejected one never became a revision.
    assert len(plans) == 1
    assert plans[0].text.startswith("# Repaired plan")
    assert plans[0].finding_responses == []
    # The repair prompt names the offending ids, so the retry is informed.
    assert len(requests) == 2
    assert "not on the review ledger" in requests[1]
    assert "F1, F2" in requests[1]


def test_planner_may_respond_to_a_finding_on_the_ledger(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    """The same check must not reject a legitimate response in a later round."""
    queue, requests = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "major", "text": "Add recovery."}]),
            _plan(responses=[{"finding_id": "F1", "status": "answered", "rationale": "Added."}], name="Second"),
        ]
    )
    session_id = _start(controller_store, settings)
    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER):
        _run(controller_store, settings, session_id, kind)

    # Three turns, three requests: no repair was triggered.
    assert len(requests) == 3
    assert controller_store.planning_findings(session_id)[0]["status"] == "answered"


def test_each_turn_records_the_model_that_produced_it(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    """The model is stored per message, not read from settings at display time.

    Planning settings can change between a turn running and someone reading it,
    so the setting is not a record of what happened.
    """
    queue, _ = scripted_runner
    queue.extend([_plan(), _review(approved=True, findings=[])])
    session_id = _start(controller_store, settings)
    _run(controller_store, settings, session_id, TurnKind.PLANNER)
    _run(controller_store, settings, session_id, TurnKind.REVIEWER)

    messages = [
        service._message_model(row)
        for row in controller_store.planning_messages(session_id)
    ]
    by_role = {message.role: message.model for message in messages}
    assert by_role[PlanningRole.PLANNER] == TEST_MODEL
    assert by_role[PlanningRole.REVIEWER] == TEST_MODEL
    # The human's opening request ran no model, so it records none.
    assert by_role[PlanningRole.USER] == ""


def test_scripted_runner_starts_no_containers_or_project_writes(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, requests = scripted_runner
    queue.extend([_plan(), _review(approved=True, findings=[])])
    session_id = _start(controller_store, settings)
    _run(controller_store, settings, session_id, TurnKind.PLANNER)
    _run(controller_store, settings, session_id, TurnKind.REVIEWER)

    # The fixture asserts the project volume on every turn; two turns ran.
    assert len(requests) == 2


def test_a_plan_spec_stored_with_a_description_still_parses(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    """Specs settled while the planner still wrote a description keep loading.

    The field is gone from the model, so an old row carries a key the model no
    longer declares. Pydantic ignores it, and this pins that: a stored spec is
    never rewritten, so every session settled before the field was dropped would
    otherwise fail to load.
    """
    queue, _ = scripted_runner
    queue.extend([_plan(), _review(approved=True, findings=[])])
    session_id = _start(controller_store, settings)
    _run(controller_store, settings, session_id, TurnKind.PLANNER)
    _run(controller_store, settings, session_id, TurnKind.REVIEWER)
    session = controller_store.planning_session(session_id)
    assert session is not None
    spec = service._json_value(session["plan_spec_json"])
    spec["description"] = "A summary the planner used to write."

    assert not hasattr(PlanSpec(**spec), "description")


def test_turn_messages_expose_the_verdict_and_the_findings_of_each_round(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    scripted_runner: tuple[deque[dict[str, Any]], list[str]],
) -> None:
    queue, _ = scripted_runner
    queue.extend(
        [
            _plan(),
            _review(approved=False, findings=[{"id": "NEW-1", "severity": "major", "text": "Add recovery."}]),
            _plan(responses=[{"finding_id": "F1", "status": "answered", "rationale": "Added recovery."}], name="Second"),
            _review(approved=True, findings=[]),
        ]
    )
    session_id = _start(controller_store, settings)

    for kind in (TurnKind.PLANNER, TurnKind.REVIEWER, TurnKind.PLANNER, TurnKind.REVIEWER):
        _run(controller_store, settings, session_id, kind)

    messages = [
        service._message_model(row)
        for row in controller_store.planning_messages(session_id)
    ]
    reviews = [message for message in messages if message.role == PlanningRole.REVIEWER]
    plans = [message for message in messages if message.role == PlanningRole.PLANNER]

    assert [review.approved for review in reviews] == [False, True]
    # The round payload keeps the reviewer's `id` key, renamed for the UI, and
    # the minted ledger id rather than the reviewer's placeholder NEW-1.
    assert [finding.finding_id for finding in reviews[0].findings] == ["F1"]
    assert reviews[0].findings[0].severity == "major"
    assert reviews[1].findings == []
    assert plans[0].finding_responses == []
    assert plans[1].finding_responses[0].finding_id == "F1"
    assert plans[1].finding_responses[0].status == "answered"
