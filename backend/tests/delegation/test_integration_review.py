import json
from pathlib import Path
from typing import Any

import pytest
from conftest import register_ready_v1_sandbox

from app.controller.store import ControllerStore, ReviewGenerating
from app.delegation import integration_review, service
from app.delegation.config import IntegrationReviewSettings
from app.delegation.delivery import FeatureTarget
from app.delegation.models import (
    DelegationStatus,
    GenerateIntegrationReviewRequest,
    IntegrationReviewStatus,
)
from app.planning.config import get_planning_settings
from app.planning.runner import TurnResult

PLAN = {
    "title": "Feature",
    "scope": "Implement the feature",
    "approach": "Add the behavior and tests",
}
BASE_COMMIT = "1" * 40
HEAD_COMMIT = "2" * 40


def _pin_target(monkeypatch: pytest.MonkeyPatch) -> None:
    target = FeatureTarget("main", BASE_COMMIT, HEAD_COMMIT)
    monkeypatch.setattr(
        integration_review,
        "capture_feature_target",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        integration_review,
        "ensure_target_unchanged",
        lambda *_args, **_kwargs: None,
    )


def _item(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Item {key}",
        "objective": "do it",
        "scope": "src/app.py",
        "dependencies": [],
        "files": ["src/app.py"],
        "write_scope": ["src/app.py"],
        "acceptance_criteria": ["behavior works"],
        "verification": [{"command_kind": "test"}],
        "complexity": "medium",
    }


@pytest.fixture
def store(tmp_path: Path) -> ControllerStore:
    result = ControllerStore(tmp_path / "controller.sqlite3")
    result.initialize()
    register_ready_v1_sandbox(
        result,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="sample-volume",
        created_at="2026-08-08T00:00:00Z",
    )
    result.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Feature",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="work",
        max_review_turns=3,
    )
    result.set_plan_spec(session_id="session-1", plan_spec=PLAN)
    result.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "model",
        }
    )
    result.settle_implementation_context(
        "context-1",
        to_status="ready",
        changes={
            "manifest_json": "{}",
            "commands_json": json.dumps(
                [
                    {
                        "kind": "test",
                        "command": "pytest",
                        "confirmed": True,
                        "reason": "defined",
                    }
                ]
            ),
        },
    )
    return result


def _completed(store: ControllerStore):
    view = service.create_revision(store, "session-1", [_item("a")])
    item = view.items[0].item
    store.claim_work_item_run(
        {
            "id": "run-1",
            "work_item_id": item.id,
            "delegation_id": view.delegation.id,
            "status": "running",
            "provider": "claude",
            "model": "model",
            "task_id": None,
        }
    )
    store.settle_work_item_run(
        "run-1",
        to_status="succeeded",
        changes={
            "result_json": json.dumps({"changed": ["added behavior"]}),
            "verification_json": json.dumps({"passed": True}),
        },
    )
    service.transition(store, view.delegation.id, DelegationStatus.RUNNING)
    return service.transition(store, view.delegation.id, DelegationStatus.COMPLETED)


def test_generating_review_uses_a_named_busy_error(
    store: ControllerStore,
) -> None:
    completed = _completed(store)
    values = {
        "id": "review-1",
        "delegation_id": completed.delegation.id,
        "status": IntegrationReviewStatus.GENERATING.value,
        "provider": "claude",
        "model": "review-model",
    }

    assert store.claim_delegation_review(values) == 1
    with pytest.raises(ReviewGenerating):
        store.claim_delegation_review({**values, "id": "review-2"})

    with pytest.raises(service.DelegationOperationError) as error:
        integration_review.claim_integration_review(
            get_planning_settings(),
            IntegrationReviewSettings("review-model"),
            store,
            completed.delegation.id,
            GenerateIntegrationReviewRequest(),
        )

    assert error.value.status_code == 409
    assert error.value.detail == "An integration review is already running"


def test_review_reads_final_repo_and_retains_result(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(store)
    store.claim_delegation_change_request(
        {
            "id": "change-1",
            "delegation_id": completed.delegation.id,
            "status": "running",
            "instructions": "Replace the button label after it is clicked",
            "provider": "claude",
            "model": "change-model",
            "task_id": None,
        }
    )
    store.settle_delegation_change_request(
        "change-1",
        to_status="awaiting_review",
        verification_json=json.dumps(
            {
                "passed": True,
                "commands": [{"command": "npm test", "passed": True}],
                "acceptance_evidence": {
                    "complete": True,
                    "errors": [],
                    "criteria": [
                        {
                            "criterion": "Only the confirmation label is visible after clicking",
                            "verified": True,
                            "evidence": "browser test",
                        }
                    ],
                },
            }
        ),
    )
    _pin_target(monkeypatch)
    calls = []

    def run_turn(_docker, settings, request):
        calls.append((settings, request))
        return TurnResult(
            raw_output="{}",
            payload={
                "approved": True,
                "summary": "The feature matches the plan",
                "findings": [],
            },
            model="reported-model",
        )

    monkeypatch.setattr(integration_review, "run_planning_turn", run_turn)

    outcome = integration_review.generate_integration_review(
        object(),
        get_planning_settings(),
        IntegrationReviewSettings("review-model"),
        store,
        completed.delegation.id,
        GenerateIntegrationReviewRequest(),
        session_id="session-1",
        project_name="sample",
    )

    assert outcome.accepted is True
    assert outcome.review.status is IntegrationReviewStatus.COMPLETED
    assert outcome.review.approved is True
    assert outcome.review.model == "reported-model"
    assert outcome.review.base_branch == "main"
    assert outcome.review.base_commit == BASE_COMMIT
    assert outcome.review.head_commit == HEAD_COMMIT
    assert calls[0][0].credential_profile == "work"
    assert calls[0][1].project_volume == "sample-volume"
    assert "controller-run verification" in calls[0][1].prompt
    assert "Replace the button label after it is clicked" in calls[0][1].prompt
    assert "A build alone" not in calls[0][1].prompt
    assert "only evidence is a build" in calls[0][1].prompt
    assert (
        service.view(store, completed.delegation.id).changes[0].status.value
        == "completed"
    )
    assert service.view(store, completed.delegation.id).review == outcome.review


def test_invalid_review_gets_one_repair_then_fails(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(store)
    _pin_target(monkeypatch)
    prompts = []

    def invalid(_docker, _settings, request):
        prompts.append(request.prompt)
        return TurnResult(raw_output="bad", payload={"approved": "yes"}, model="model")

    monkeypatch.setattr(integration_review, "run_planning_turn", invalid)

    outcome = integration_review.generate_integration_review(
        object(),
        get_planning_settings(),
        IntegrationReviewSettings("review-model"),
        store,
        completed.delegation.id,
        GenerateIntegrationReviewRequest(),
    )

    assert outcome.accepted is False
    assert outcome.attempts == 2
    assert outcome.review.status is IntegrationReviewStatus.FAILED
    assert "Your previous reply was rejected:" in prompts[1]


def test_review_cannot_approve_a_change_without_acceptance_evidence(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(store)
    store.claim_delegation_change_request(
        {
            "id": "change-without-evidence",
            "delegation_id": completed.delegation.id,
            "status": "running",
            "instructions": "Change the interactive button state",
            "provider": "claude",
            "model": "change-model",
            "task_id": None,
        }
    )
    store.settle_delegation_change_request(
        "change-without-evidence",
        to_status="awaiting_review",
        verification_json=json.dumps(
            {
                "passed": True,
                "acceptance_evidence": {
                    "complete": False,
                    "errors": ["Agent reported no observable acceptance criteria"],
                },
            }
        ),
    )
    _pin_target(monkeypatch)

    monkeypatch.setattr(
        integration_review,
        "run_planning_turn",
        lambda *_args, **_kwargs: TurnResult(
            raw_output="{}",
            payload={"approved": True, "summary": "Looks correct", "findings": []},
            model="review-model",
        ),
    )

    outcome = integration_review.generate_integration_review(
        object(),
        get_planning_settings(),
        IntegrationReviewSettings("review-model"),
        store,
        completed.delegation.id,
        GenerateIntegrationReviewRequest(),
    )

    assert outcome.review.approved is False
    assert outcome.review.findings[0].severity == "high"
    assert "lacks complete acceptance evidence" in outcome.review.findings[0].text
    assert service.view(store, completed.delegation.id).changes[0].status.value == (
        "awaiting_review"
    )


def test_a_later_change_supersedes_an_earlier_incomplete_one(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answered revision must stop blocking the delegation.

    A change request has no settled state to move to, so every revision stays
    at awaiting_review for good. Judging all of them let one weak turn block
    approval permanently, even after a later turn fixed exactly what it missed.
    """
    completed = _completed(store)
    for revision, evidence in (
        (
            1,
            {
                "complete": False,
                "errors": ["Agent reported no observable acceptance criteria"],
            },
        ),
        (2, {"complete": True, "errors": []}),
    ):
        request_id = f"change-{revision}"
        store.claim_delegation_change_request(
            {
                "id": request_id,
                "delegation_id": completed.delegation.id,
                "status": "running",
                "instructions": "Change the interactive button state",
                "provider": "claude",
                "model": "change-model",
                "task_id": None,
            }
        )
        store.settle_delegation_change_request(
            request_id,
            to_status="awaiting_review",
            verification_json=json.dumps(
                {"passed": True, "acceptance_evidence": evidence}
            ),
        )
    _pin_target(monkeypatch)

    monkeypatch.setattr(
        integration_review,
        "run_planning_turn",
        lambda *_args, **_kwargs: TurnResult(
            raw_output="{}",
            payload={"approved": True, "summary": "Looks correct", "findings": []},
            model="review-model",
        ),
    )

    outcome = integration_review.generate_integration_review(
        object(),
        get_planning_settings(),
        IntegrationReviewSettings("review-model"),
        store,
        completed.delegation.id,
        GenerateIntegrationReviewRequest(),
    )

    assert outcome.review.approved is True
    assert outcome.review.findings == []


def _approving_review(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
    findings: list[dict[str, Any]],
):
    completed = _completed(store)
    _pin_target(monkeypatch)
    monkeypatch.setattr(
        integration_review,
        "run_planning_turn",
        lambda *_args, **_kwargs: TurnResult(
            raw_output="{}",
            payload={
                "approved": True,
                "summary": "Looks correct",
                "findings": findings,
            },
            model="review-model",
        ),
    )
    return integration_review.generate_integration_review(
        object(),
        get_planning_settings(),
        IntegrationReviewSettings("review-model"),
        store,
        completed.delegation.id,
        GenerateIntegrationReviewRequest(),
    )


def test_approval_is_overridden_by_a_medium_finding(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _approving_review(
        store,
        monkeypatch,
        [
            {
                "severity": "medium",
                "text": "The error path is unhandled",
                "work_item_keys": ["a"],
            }
        ],
    )

    assert outcome.review.approved is False
    assert "overridden" in outcome.review.summary
    assert "Looks correct" in outcome.review.summary
    assert len(outcome.review.findings) == 1


def test_a_low_finding_still_approves(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _approving_review(
        store,
        monkeypatch,
        [
            {
                "severity": "low",
                "text": "The comment has a typo",
                "work_item_keys": ["a"],
            }
        ],
    )

    assert outcome.review.approved is True
    assert outcome.review.summary == "Looks correct"


def test_review_requires_completed_delegation(store: ControllerStore) -> None:
    view = service.create_revision(store, "session-1", [_item("a")])

    with pytest.raises(service.DelegationOperationError) as error:
        integration_review.generate_integration_review(
            object(),
            get_planning_settings(),
            IntegrationReviewSettings("review-model"),
            store,
            view.delegation.id,
            GenerateIntegrationReviewRequest(),
        )

    assert error.value.status_code == 409
