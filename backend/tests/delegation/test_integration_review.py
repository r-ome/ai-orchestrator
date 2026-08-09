import json
from pathlib import Path
from typing import Any

import pytest

from app.controller.store import ControllerStore
from app.delegation import integration_review, service
from app.delegation.config import IntegrationReviewSettings
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
    result.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        source_path="/projects/sample",
        volume_name="sample-volume",
        status="ready",
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
                [{"kind": "test", "command": "pytest", "confirmed": True, "reason": "defined"}]
            ),
        },
    )
    return result


def _completed(store: ControllerStore):
    view = service.create_revision(store, "session-1", [_item("a")])
    item = view.items[0].item
    store.start_work_item_run(
        {
            "id": "run-1",
            "work_item_id": item.id,
            "delegation_id": view.delegation.id,
            "attempt": 1,
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


def test_review_reads_final_repo_and_retains_result(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(store)
    calls = []

    def run_turn(_docker, settings, request):
        calls.append((settings, request))
        return TurnResult(
            raw_output="{}",
            payload={"approved": True, "summary": "The feature matches the plan", "findings": []},
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
    assert calls[0][0].credential_profile == "work"
    assert calls[0][1].project_volume == "sample-volume"
    assert "controller-run verification" in calls[0][1].prompt
    assert service.view(store, completed.delegation.id).review == outcome.review


def test_invalid_review_gets_one_repair_then_fails(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(store)
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
    assert "previous response was invalid" in prompts[1]


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
