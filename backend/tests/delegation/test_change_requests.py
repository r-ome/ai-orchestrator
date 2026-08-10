import json
from pathlib import Path
from typing import Any

import pytest

from app.controller.store import ControllerStore
from app.delegation import change_requests, service
from app.delegation.models import (
    ChangeRequestStatus,
    DelegationStatus,
    RequestFeatureChange,
)
from app.implementation_context.models import ContextStatus
from app.planning.models import PlanningStatus
from app.tasks.config import CodingTurnSettings
from app.tasks.models import Task, TaskRunResponse, TaskStatus, TurnUsageView


SETTINGS = CodingTurnSettings(
    timeout_seconds=900,
    memory="4g",
    pids_limit=512,
    max_log_bytes=2_000_000,
    claude_model="claude-sonnet-5",
    codex_model="gpt-5.6-terra",
    codex_reasoning_effort="medium",
    credential_profile="default",
)


def _store(tmp_path: Path) -> tuple[ControllerStore, str]:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        source_path="/projects/sample",
        volume_name="sample-volume",
        status="ready",
        created_at="2026-08-10T00:00:00Z",
    )
    store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Feature",
        status=PlanningStatus.PLAN_READY.value,
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.set_plan_spec(
        session_id="session-1",
        plan_spec={"title": "Feature", "plan_markdown": "# Feature"},
    )
    store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": ContextStatus.GENERATING.value,
            "provider": "claude",
            "model": "model",
        }
    )
    store.settle_implementation_context(
        "context-1",
        to_status=ContextStatus.READY.value,
        changes={
            "manifest_json": json.dumps(
                {
                    "modules": [
                        {
                            "path": "src/components/EmptyState.tsx",
                            "purpose": "Renders the empty state",
                        }
                    ]
                }
            ),
            "commands_json": json.dumps(
                [
                    {
                        "kind": "test",
                        "command": "npm test",
                        "confirmed": True,
                        "reason": "defined",
                    }
                ]
            )
        },
    )
    view = service.create_revision(
        store,
        "session-1",
        [
            {
                "key": "a",
                "title": "A",
                "objective": "A",
                "scope": "A",
                "dependencies": [],
                "acceptance_criteria": ["done"],
                "verification": [{"command_kind": "test"}],
                "complexity": "low",
            }
        ],
        project_name="sample",
    )
    store.transition_delegation(
        view.delegation.id,
        to_status=DelegationStatus.RUNNING.value,
        from_statuses=[DelegationStatus.READY.value],
    )
    store.transition_delegation(
        view.delegation.id,
        to_status=DelegationStatus.COMPLETED.value,
        from_statuses=[DelegationStatus.RUNNING.value],
        terminal=True,
    )
    return store, view.delegation.id


def _task_stubs(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    verification_passes: bool,
    agent_result: dict[str, Any] | None = None,
) -> None:
    reported_result = agent_result or {
        "changed": ["empty state"],
        "change_kind": "interactive_ui",
        "acceptance_criteria": [
            {
                "criterion": "The empty state shows the tightened message",
                "verification_kind": "behavior_test",
                "verified": True,
                "evidence": "npm test exercises the rendered state",
            }
        ],
        "verification": {
            "ran": ["npm test"],
            "outcome": "passed",
            "detail": "Rendered behavior passed",
        },
    }

    def start_task(_client: Any, _store: ControllerStore, request: Any) -> Task:
        task_id = "a" * 32
        store.create_task(
            task_id=task_id,
            sandbox_id="sandbox-1",
            agent_run_id=None,
            branch=f"task/{task_id}",
            base_branch="main",
            base_commit="1" * 40,
            title=request.title,
            status=TaskStatus.OPEN.value,
        )
        return Task.model_validate(store.task(task_id))

    def run_task(
        _client: Any,
        _store: ControllerStore,
        _settings: CodingTurnSettings,
        task_id: str,
        request: Any,
    ) -> TaskRunResponse:
        assert "Tighten the empty state" in request.prompt
        assert "observable acceptance criteria" in request.prompt
        assert "src/components/EmptyState.tsx" in request.prompt
        assert '"reviewed_plan"' in request.prompt
        store.advance_task_status(
            task_id=task_id,
            from_statuses=[TaskStatus.OPEN.value],
            to_status=TaskStatus.REPORTED.value,
            head_commit="2" * 40,
        )
        return TaskRunResponse(
            task=Task.model_validate(store.task(task_id)),
            turn_status="succeeded",
            committed=True,
            model=request.model,
            usage=TurnUsageView(),
            result=reported_result,
        )

    def verify_task(_store: ControllerStore, task_id: str, **_kwargs: Any) -> Task:
        store.advance_task_status(
            task_id=task_id,
            from_statuses=[TaskStatus.REPORTED.value],
            to_status=TaskStatus.REVIEW.value,
        )
        return Task.model_validate(store.task(task_id))

    def accept_task(_client: Any, _store: ControllerStore, task_id: str) -> Task:
        store.advance_task_status(
            task_id=task_id,
            from_statuses=[TaskStatus.REVIEW.value],
            to_status=TaskStatus.ACCEPTED.value,
            settled=True,
        )
        return Task.model_validate(store.task(task_id))

    def reject_task(_client: Any, _store: ControllerStore, task_id: str) -> Task:
        store.advance_task_status(
            task_id=task_id,
            from_statuses=[TaskStatus.REPORTED.value],
            to_status=TaskStatus.REJECTED.value,
            settled=True,
        )
        return Task.model_validate(store.task(task_id))

    monkeypatch.setattr(change_requests, "start_task", start_task)
    monkeypatch.setattr(change_requests, "run_task", run_task)
    monkeypatch.setattr(change_requests, "verify_task", verify_task)
    monkeypatch.setattr(change_requests, "accept_task", accept_task)
    monkeypatch.setattr(change_requests, "reject_task", reject_task)
    monkeypatch.setattr(
        change_requests,
        "run_verification",
        lambda *_args, **_kwargs: {
            "passed": verification_passes,
            "commands": [
                {
                    "command": "npm test",
                    "passed": verification_passes,
                    "detail": "Passed" if verification_passes else "Exited with status 1",
                }
            ],
        },
    )


def test_requested_changes_wait_for_whole_feature_review_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, delegation_id = _store(tmp_path)
    _task_stubs(store, monkeypatch, verification_passes=True)
    claim = change_requests.claim_change_request(
        object(),
        SETTINGS,
        store,
        delegation_id,
        RequestFeatureChange(instructions="Tighten the empty state"),
        session_id="session-1",
        project_name="sample",
    )

    result = change_requests.execute_change_request(object(), store, claim)

    assert result.status is ChangeRequestStatus.AWAITING_REVIEW
    assert result.verification and result.verification["passed"] is True
    assert result.verification["acceptance_evidence"]["complete"] is True
    stored = store.delegation_change_request(claim.request_id)
    assert stored is not None
    assert "src/components/EmptyState.tsx" in stored["prompt"]
    assert store.task(claim.task_id)["status"] == TaskStatus.ACCEPTED.value
    assert service.view(store, delegation_id).changes[0].instructions == "Tighten the empty state"


def test_missing_behavior_evidence_is_retained_for_whole_feature_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, delegation_id = _store(tmp_path)
    _task_stubs(
        store,
        monkeypatch,
        verification_passes=True,
        agent_result={
            "changed": ["empty state"],
            "change_kind": "interactive_ui",
            "verification": {
                "ran": ["npm run build"],
                "outcome": "passed",
                "detail": "No browser check was run",
            },
        },
    )
    claim = change_requests.claim_change_request(
        object(),
        SETTINGS,
        store,
        delegation_id,
        RequestFeatureChange(instructions="Tighten the empty state"),
        session_id="session-1",
        project_name="sample",
    )

    result = change_requests.execute_change_request(object(), store, claim)

    assert result.status is ChangeRequestStatus.AWAITING_REVIEW
    assert result.verification is not None
    evidence = result.verification["acceptance_evidence"]
    assert evidence["complete"] is False
    assert evidence["errors"] == [
        "Agent reported no observable acceptance criteria",
        "Behavioral change has no behavioral verification check",
    ]
    assert store.task(claim.task_id)["status"] == TaskStatus.ACCEPTED.value


def test_build_only_evidence_cannot_verify_an_interactive_change() -> None:
    evidence = change_requests._acceptance_evidence(
        {
            "change_kind": "interactive_ui",
            "acceptance_criteria": [
                {
                    "criterion": "The button replaces its label after clicking",
                    "verification_kind": "static_check",
                    "verified": True,
                    "evidence": "npm run build passed",
                }
            ],
            "verification": {
                "ran": ["npm run build"],
                "outcome": "passed",
            },
        }
    )

    assert evidence["complete"] is False
    assert evidence["errors"] == [
        "acceptance_criteria[0] lacks a behavioral test",
        "Behavioral change has no behavioral verification check",
    ]


def test_browser_installation_cannot_count_as_behavior_verification() -> None:
    evidence = change_requests._acceptance_evidence(
        {
            "change_kind": "interactive_ui",
            "acceptance_criteria": [
                {
                    "criterion": "The button replaces its label after clicking",
                    "verification_kind": "behavior_test",
                    "verified": True,
                    "evidence": "Playwright was installed",
                }
            ],
            "verification": {
                "ran": ["npx playwright install chromium"],
                "outcome": "passed",
            },
        }
    )

    assert evidence["complete"] is False
    assert evidence["errors"] == [
        "Agent attempted to install test infrastructure",
        "Behavioral change has no behavioral verification check",
    ]


def test_failed_change_verification_keeps_the_previous_implementation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, delegation_id = _store(tmp_path)
    _task_stubs(store, monkeypatch, verification_passes=False)
    claim = change_requests.claim_change_request(
        object(),
        SETTINGS,
        store,
        delegation_id,
        RequestFeatureChange(instructions="Tighten the empty state"),
        session_id="session-1",
        project_name="sample",
    )

    with pytest.raises(service.DelegationOperationError):
        change_requests.execute_change_request(object(), store, claim)

    change = service.view(store, delegation_id).changes[0]
    assert change.status is ChangeRequestStatus.FAILED
    assert store.task(claim.task_id)["status"] == TaskStatus.REJECTED.value
