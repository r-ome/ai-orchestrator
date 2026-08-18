import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.controller.store import ControllerStore, DelegationActive, RunActive
from app.delegation import service
from app.delegation.models import (
    ACTIVE_DELEGATION_STATUSES,
    DELEGATION_TRANSITIONS,
    TERMINAL_DELEGATION_STATUSES,
    DelegationStatus,
    RunStatus,
    WorkItemState,
    SetRoutingRequest,
)
from app.delegation.routing import ProviderModels, RoutingSettings
from app.implementation_context.models import ContextStatus
from app.planning.models import PlanningStatus


PLAN = {
    "title": "Do it",
    "scope": "Do the requested work",
    "approach": "Use the existing structure",
    "components": [{"name": "component", "responsibility": "does it"}],
    "risks": [],
    "open_questions": [],
    "reviewer_outcome": {"approved": True, "rounds": 1},
    "plan_markdown": "# Plan",
    "confirmed_understanding": True,
    "generated_at": "2026-08-08T00:00:00Z",
}


def _item(key: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "title": f"Item {key}",
        "objective": "do the thing",
        "scope": "just the thing",
        "out_of_scope": "everything else",
        "dependencies": [],
        "files": ["src/app.ts"],
        "symbols": ["compute"],
        "write_scope": ["src/app.ts"],
        "acceptance_criteria": ["the thing is done"],
        "verification": [{"command_kind": "build", "reason": "it compiles"}],
        "complexity": "medium",
        "architecture": ["utilities live in src/utils"],
        "risks": ["none"],
    }
    item.update(overrides)
    return item


def _create_session(
    store: ControllerStore,
    *,
    session_id: str = "session-1",
    status: PlanningStatus = PlanningStatus.PLAN_READY,
) -> None:
    store.create_planning_session(
        session_id=session_id,
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Do it",
        status=status.value,
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.set_plan_spec(session_id=session_id, plan_spec=PLAN)


def _add_context(store: ControllerStore, session_id: str = "session-1") -> None:
    store.start_implementation_context(
        {
            "id": f"context-{session_id}",
            "session_id": session_id,
            "sandbox_id": "sandbox-1",
            "status": ContextStatus.GENERATING.value,
            "provider": "claude",
            "model": "claude-sonnet-5",
        }
    )
    store.settle_implementation_context(
        f"context-{session_id}",
        to_status=ContextStatus.READY.value,
        changes={
            "manifest_json": json.dumps(
                {
                    "modules": [{"path": "src/app.ts", "purpose": "application"}],
                    "commands": {"build": "npm run build"},
                }
            ),
            "commands_json": json.dumps(
                [
                    {
                        "kind": "build",
                        "command": "npm run build",
                        "confirmed": True,
                        "reason": "defined",
                    },
                    {
                        "kind": "test",
                        "command": "npm test",
                        "confirmed": False,
                        "reason": "missing",
                    },
                ]
            ),
        },
    )


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
    _create_session(controller_store)
    _add_context(controller_store)
    return controller_store


def test_every_active_delegation_status_can_reach_a_terminal_status() -> None:
    for start in ACTIVE_DELEGATION_STATUSES:
        seen: set[DelegationStatus] = set()
        frontier = [start]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(DELEGATION_TRANSITIONS[current])
        assert seen & TERMINAL_DELEGATION_STATUSES


def test_only_confirmed_commands_reach_graph_validation(
    store: ControllerStore,
) -> None:
    assert service.confirmed_command_kinds(store, "session-1") == {"build"}


def test_revision_and_work_items_are_stored_atomically(store: ControllerStore) -> None:
    result = service.create_revision(
        store,
        "session-1",
        [_item("a"), _item("b", dependencies=["a"])],
    )

    assert result.delegation.revision == 1
    assert result.delegation.context_id == "context-session-1"
    assert [entry.item.key for entry in result.items] == ["a", "b"]
    assert result.items[0].item.files == ["src/app.ts"]
    assert result.items[0].item.verification[0].command_kind == "build"


def test_invalid_graph_and_unconfirmed_command_write_nothing(
    store: ControllerStore,
) -> None:
    with pytest.raises(service.DelegationOperationError) as cycle_error:
        service.create_revision(
            store,
            "session-1",
            [_item("a", dependencies=["b"]), _item("b", dependencies=["a"])],
        )
    with pytest.raises(service.DelegationOperationError) as command_error:
        service.create_revision(
            store,
            "session-1",
            [_item("a", verification=[{"command_kind": "test"}])],
        )

    assert cycle_error.value.status_code == 422
    assert command_error.value.status_code == 422
    assert service.list_delegations(store, "session-1") == []


def test_waves_readiness_and_parallel_candidates_are_derived(
    store: ControllerStore,
) -> None:
    result = service.create_revision(
        store,
        "session-1",
        [_item("a"), _item("b"), _item("c", dependencies=["a", "b"])],
    )
    by_key = {entry.item.key: entry for entry in result.items}

    assert result.waves == [["a", "b"], ["c"]]
    assert result.ready == ["a", "b"]
    assert by_key["c"].state is WorkItemState.BLOCKED
    assert by_key["c"].blocked_by == ["a", "b"]
    assert by_key["a"].can_run_in_parallel_with == ["b"]


def test_run_attempts_drive_item_state_and_retain_metrics(store: ControllerStore) -> None:
    result = service.create_revision(
        store,
        "session-1",
        [_item("a"), _item("b", dependencies=["a"])],
    )
    item_a = result.items[0].item
    store.claim_work_item_run(
        {
            "id": "run-1",
            "work_item_id": item_a.id,
            "delegation_id": result.delegation.id,
            "status": RunStatus.RUNNING.value,
            "provider": "claude",
            "model": "model",
            "task_id": None,
        }
    )

    running = service.view(store, result.delegation.id)
    assert running.items[0].state is WorkItemState.RUNNING
    assert running.ready == []

    store.settle_work_item_run(
        "run-1",
        to_status=RunStatus.SUCCEEDED.value,
        changes={"cost_usd": 0.02, "input_tokens": 10, "duration_ms": 900},
    )
    settled = service.view(store, result.delegation.id)
    by_key = {entry.item.key: entry for entry in settled.items}

    assert by_key["a"].state is WorkItemState.COMPLETED
    assert by_key["b"].state is WorkItemState.READY
    assert by_key["a"].runs[0].usage.cost_usd == 0.02


def test_attempts_append_and_one_run_can_be_active(store: ControllerStore) -> None:
    result = service.create_revision(store, "session-1", [_item("a"), _item("b")])
    first, second = [entry.item for entry in result.items]
    assert store.claim_work_item_run(
        {
            "id": "run-a",
            "work_item_id": first.id,
            "delegation_id": result.delegation.id,
            "status": "running",
            "provider": "claude",
            "model": "model",
            "task_id": None,
        }
    ) == 1

    with pytest.raises(RunActive):
        store.claim_work_item_run(
            {
                "id": "run-b",
                "work_item_id": second.id,
                "delegation_id": result.delegation.id,
                "status": "running",
                "provider": "claude",
                "model": "model",
                "task_id": None,
            }
        )

    assert store.settle_work_item_run("run-a", to_status="failed")
    assert store.settle_work_item_run("run-a", to_status="succeeded") is None
    assert store.claim_work_item_run(
        {
            "id": "run-c",
            "work_item_id": first.id,
            "delegation_id": result.delegation.id,
            "status": "running",
            "provider": "claude",
            "model": "model",
            "task_id": None,
        }
    ) == 2


def test_work_item_key_integrity_error_stays_raw(store: ControllerStore) -> None:
    delegation_id = "delegation-duplicate-key"
    item = _item("duplicate")

    with pytest.raises(sqlite3.IntegrityError) as caught:
        store.claim_delegation_revision(
            {
                "id": delegation_id,
                "session_id": "session-1",
                "sandbox_id": "sandbox-1",
                "context_id": "context-session-1",
                "status": DelegationStatus.READY.value,
            },
            [
                service._work_item_row(delegation_id, 0, item),
                service._work_item_row(delegation_id, 1, item),
            ],
        )

    assert str(caught.value) == "UNIQUE constraint failed: work_items.delegation_id, work_items.key"


def test_active_delegation_blocks_revision_until_settled(store: ControllerStore) -> None:
    first = service.create_revision(store, "session-1", [_item("a")])

    with pytest.raises(DelegationActive):
        store.claim_delegation_revision(
            {
                "id": "delegation-busy",
                "session_id": "session-1",
                "sandbox_id": "sandbox-1",
                "context_id": "context-session-1",
                "status": DelegationStatus.READY.value,
            },
            [],
        )

    with pytest.raises(service.DelegationOperationError) as error:
        service.create_revision(store, "session-1", [_item("a")])

    assert error.value.status_code == 409
    assert error.value.detail == "This sandbox already has an active delegation"
    service.transition(store, first.delegation.id, DelegationStatus.ABANDONED)
    second = service.create_revision(store, "session-1", [_item("a")])
    assert second.delegation.revision == 2


def test_halted_delegation_can_resume_and_terminal_one_cannot(
    store: ControllerStore,
) -> None:
    result = service.create_revision(store, "session-1", [_item("a")])
    service.transition(store, result.delegation.id, DelegationStatus.RUNNING)
    halted = service.transition(
        store,
        result.delegation.id,
        DelegationStatus.HALTED,
        error="needs attention",
    )
    resumed = service.transition(
        store,
        result.delegation.id,
        DelegationStatus.RUNNING,
    )
    service.transition(store, result.delegation.id, DelegationStatus.ABANDONED)

    assert halted.delegation.status is DelegationStatus.HALTED
    assert halted.delegation.settled_at is None
    assert resumed.delegation.status is DelegationStatus.RUNNING
    with pytest.raises(service.DelegationOperationError):
        service.transition(store, result.delegation.id, DelegationStatus.RUNNING)


def test_illegal_transition_is_refused(store: ControllerStore) -> None:
    result = service.create_revision(store, "session-1", [_item("a")])

    with pytest.raises(service.DelegationOperationError) as error:
        service.transition(store, result.delegation.id, DelegationStatus.COMPLETED)

    assert error.value.status_code == 409


def test_review_limit_session_can_be_delegated(store: ControllerStore) -> None:
    _create_session(
        store,
        session_id="session-limited",
        status=PlanningStatus.REVIEW_LIMIT_REACHED,
    )
    _add_context(store, "session-limited")

    result = service.create_revision(store, "session-limited", [_item("a")])

    assert result.delegation.revision == 1


def test_incomplete_plan_and_wrong_project_are_hidden(store: ControllerStore) -> None:
    _create_session(
        store,
        session_id="session-open",
        status=PlanningStatus.CLARIFYING,
    )

    with pytest.raises(service.DelegationOperationError) as incomplete:
        service.create_revision(store, "session-open", [_item("a")])
    with pytest.raises(service.DelegationOperationError) as wrong_project:
        service.list_delegations(store, "session-1", project_name="other")

    assert incomplete.value.status_code == 409
    assert wrong_project.value.status_code == 404


def test_routing_override_is_separate_revisable_state(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_routing_settings",
        lambda: RoutingSettings(
            claude=ProviderModels(
                "small", "standard", "strong", "standard", ("strong", "standard", "small")
            ),
            codex=ProviderModels(
                "gpt-small",
                "gpt-standard",
                "gpt-strong",
                "gpt-standard",
                ("gpt-strong", "gpt-standard", "gpt-small"),
            ),
        ),
    )
    created = service.create_revision(store, "session-1", [_item("a", complexity="high")])

    overridden = service.set_routing(
        store,
        created.delegation.id,
        "a",
        SetRoutingRequest(model="small", actor="human"),
        session_id="session-1",
        project_name="sample",
    )
    cleared = service.clear_routing(store, created.delegation.id, "a")

    assert overridden.items[0].routing
    assert overridden.items[0].routing.model == "small"
    assert overridden.items[0].routing.source == "item_override"
    assert overridden.items[0].routing.warning
    assert cleared.items[0].routing
    assert cleared.items[0].routing.model == "strong"
