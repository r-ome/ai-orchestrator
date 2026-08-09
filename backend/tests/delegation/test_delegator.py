import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.delegation import service
from app.delegation.config import DelegatorSettings
from app.delegation.models import (
    DelegationStatus,
    GenerateDelegationRequest,
)
from app.implementation_context.models import ContextStatus
from app.planning.config import PlanningSettings
from app.planning.runner import PlanningTurnError, TurnResult


DELEGATOR_SETTINGS = DelegatorSettings(
    model="claude-sonnet-5",
)
PLANNING_SETTINGS = PlanningSettings(
    clarifier_provider=AgentProvider.CLAUDE,
    planner_provider=AgentProvider.CLAUDE,
    reviewer_provider=AgentProvider.CODEX,
    credential_profile="default",
    max_review_turns=3,
    turn_timeout_seconds=600,
    planning_memory="2g",
    claude_model="opus",
    codex_model="gpt-5.6-terra",
    codex_reasoning_effort="high",
)
PLAN = {
    "title": "Add reading time",
    "scope": "Add reading time",
    "approach": "Compute from the body",
    "components": [{"name": "utility", "responsibility": "computes it"}],
    "risks": [],
    "open_questions": [],
    "reviewer_outcome": {"approved": True, "rounds": 1},
    "plan_markdown": "# Plan",
    "confirmed_understanding": True,
    "generated_at": "2026-08-08T00:00:00Z",
}
MANIFEST = {
    "modules": [{"path": "src/utils/blog.ts", "purpose": "post helpers"}],
    "symbols": [],
    "architecture": [],
    "patterns": [],
    "constraints": [],
    "assumptions": [],
    "commands": {"build": "npm run build"},
}


def _item(key: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "title": f"Item {key}",
        "objective": "do the thing",
        "scope": "just the thing",
        "dependencies": [],
        "acceptance_criteria": ["done"],
        "verification": [{"command_kind": "build", "reason": "it compiles"}],
        "complexity": "low",
    }
    item.update(overrides)
    return item


@pytest.fixture
def store(tmp_path: Path) -> ControllerStore:
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    controller_store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        source_path="/projects/sample",
        volume_name="sample-volume",
        status="ready",
        created_at="2026-08-08T00:00:00Z",
    )
    controller_store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Add reading time",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="profile-1",
        max_review_turns=3,
    )
    controller_store.set_plan_spec(session_id="session-1", plan_spec=PLAN)
    return controller_store


def _with_context(store: ControllerStore) -> None:
    store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": ContextStatus.GENERATING.value,
            "provider": "claude",
            "model": "claude-sonnet-5",
        }
    )
    store.settle_implementation_context(
        "context-1",
        to_status=ContextStatus.READY.value,
        changes={
            "manifest_json": json.dumps(MANIFEST),
            "commands_json": json.dumps(
                [
                    {
                        "kind": "build",
                        "command": "npm run build",
                        "confirmed": True,
                        "reason": "defined",
                    }
                ]
            ),
        },
    )


class _Turns:
    def __init__(self) -> None:
        self.queue: list[TurnResult | Exception] = []
        self.prompts: list[str] = []
        self.settings: list[PlanningSettings] = []

    def __call__(
        self,
        _docker_client: Any,
        settings: PlanningSettings,
        request: Any,
    ) -> TurnResult:
        self.prompts.append(request.prompt)
        self.settings.append(settings)
        queued = self.queue.pop(0) if self.queue else _result({"items": [_item("a")]})
        if isinstance(queued, Exception):
            raise queued
        return queued


def _result(payload: dict[str, Any]) -> TurnResult:
    return TurnResult(raw_output=json.dumps(payload), payload=payload, model="delegator-model")


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> _Turns:
    stub = _Turns()
    monkeypatch.setattr(service, "run_planning_turn", stub)
    return stub


def _generate(
    store: ControllerStore,
    request: GenerateDelegationRequest | None = None,
    *,
    project_name: str | None = None,
) -> Any:
    return service.generate_revision(
        object(),
        PLANNING_SETTINGS,
        DELEGATOR_SETTINGS,
        store,
        "session-1",
        request or GenerateDelegationRequest(),
        project_name=project_name,
    )


def test_valid_decomposition_becomes_revision_one(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    turns.queue.append(
        _result({"items": [_item("a"), _item("b", dependencies=["a"])]})
    )

    outcome = _generate(store)

    assert outcome.accepted
    assert outcome.attempts == 1
    assert outcome.delegation is not None
    assert outcome.delegation.delegation.revision == 1
    assert outcome.delegation.delegation.status is DelegationStatus.READY
    assert outcome.delegation.waves == [["a"], ["b"]]
    assert outcome.delegation.ready == ["a"]


def test_prompt_carries_plan_context_and_confirmed_kinds(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)

    _generate(store)

    prompt = turns.prompts[0]
    assert "Compute from the body" in prompt
    assert "src/utils/blog.ts" in prompt
    assert "['build']" in prompt
    assert turns.settings[0].credential_profile == "profile-1"
    assert turns.settings[0].claude_model == "claude-sonnet-5"


def test_requested_codex_model_is_used(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)

    _generate(
        store,
        GenerateDelegationRequest(provider=AgentProvider.CODEX, model="gpt-test"),
    )

    assert turns.settings[0].codex_model == "gpt-test"


def test_invalid_graph_is_repaired_once_then_accepted(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    turns.queue.extend(
        [
            _result({"items": [_item("a", dependencies=["ghost"])]}),
            _result({"items": [_item("a")]}),
        ]
    )

    outcome = _generate(store)

    assert outcome.accepted
    assert outcome.attempts == 2
    assert "ghost" in turns.prompts[1]
    assert "not a work item" in turns.prompts[1]


def test_invalid_graph_after_repair_writes_nothing(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    cyclic = _result(
        {
            "items": [
                _item("a", dependencies=["b"]),
                _item("b", dependencies=["a"]),
            ]
        }
    )
    turns.queue.extend([cyclic, cyclic])

    outcome = _generate(store)

    assert not outcome.accepted
    assert outcome.delegation is None
    assert any("cycle" in error for error in outcome.validation_errors)
    assert service.list_delegations(store, "session-1") == []


def test_unconfirmed_verification_kind_is_rejected(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    bad = _result(
        {"items": [_item("a", verification=[{"command_kind": "test"}])]}
    )
    turns.queue.extend([bad, bad])

    outcome = _generate(store)

    assert not outcome.accepted
    assert any("not confirmed" in error for error in outcome.validation_errors)


def test_invalid_json_output_is_reported_without_writing(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    failure = PlanningTurnError(422, "No JSON object found", "not json")
    turns.queue.extend([failure, failure])

    outcome = _generate(store)

    assert not outcome.accepted
    assert outcome.turn_status == "invalid_output"
    assert outcome.turn_error == "No JSON object found"
    assert service.list_delegations(store, "session-1") == []


def test_payload_without_items_is_rejected(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    wrong = _result({"work_items": [_item("a")]})
    turns.queue.extend([wrong, wrong])

    outcome = _generate(store)

    assert not outcome.accepted
    assert any("'items' is required" in error for error in outcome.validation_errors)


def test_missing_context_and_active_delegation_stop_before_turn(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    with pytest.raises(service.DelegationOperationError) as missing:
        _generate(store)
    assert turns.prompts == []

    _with_context(store)
    _generate(store)
    spent = len(turns.prompts)
    with pytest.raises(service.DelegationOperationError) as active:
        _generate(store)

    assert missing.value.status_code == 409
    assert active.value.status_code == 409
    assert len(turns.prompts) == spent


def test_context_without_confirmed_commands_stops_before_turn(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    store.start_implementation_context(
        {
            "id": "context-empty",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "model",
        }
    )
    store.settle_implementation_context(
        "context-empty",
        to_status="ready",
        changes={
            "manifest_json": json.dumps(MANIFEST),
            "commands_json": "[]",
        },
    )

    with pytest.raises(service.DelegationOperationError) as error:
        _generate(store)

    assert error.value.status_code == 409
    assert "verification commands" in error.value.detail
    assert turns.prompts == []


def test_settled_delegation_allows_new_generated_revision(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)
    first = _generate(store)
    assert first.delegation is not None
    service.transition(
        store,
        first.delegation.delegation.id,
        DelegationStatus.ABANDONED,
    )

    second = _generate(store)

    assert second.accepted
    assert second.delegation is not None
    assert second.delegation.delegation.revision == 2


def test_wrong_project_is_hidden_before_turn(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _with_context(store)

    with pytest.raises(service.DelegationOperationError) as error:
        _generate(store, project_name="other")

    assert error.value.status_code == 404
    assert turns.prompts == []
