import json
from pathlib import Path
from typing import Any

import pytest

from conftest import register_ready_v1_sandbox

from app.controller.store import ControllerStore, RunActive
from app.delegation import execution, service
from app.delegation.models import (
    DelegationStatus,
    FailureKind,
    RunStatus,
    StartRunRequest,
    WorkItemState,
)
from app.implementation_context.models import ContextStatus
from app.planning.models import PlanningStatus
from app.tasks.config import CodingTurnSettings
from app.tasks.models import Task, TaskRunResponse, TaskStatus, TurnUsageView
from app.tasks.service import TaskOperationError


SETTINGS = CodingTurnSettings(
    timeout_seconds=900,
    memory="4g",
    max_log_bytes=2_000_000,
    claude_model="claude-sonnet-5",
    codex_model="gpt-5.6-terra",
    codex_reasoning_effort="medium",
    credential_profile="default",
)
PLAN = {
    "title": "Add tags",
    "scope": "Add tags",
    "approach": "Schema then pages",
    "components": [],
    "risks": [],
    "open_questions": [],
    "reviewer_outcome": {"approved": True, "rounds": 1},
    "plan_markdown": "# Full plan",
    "confirmed_understanding": True,
    "generated_at": "2026-08-08T00:00:00Z",
}
MANIFEST = {
    "modules": [{"path": "src/x.ts", "purpose": "tags"}],
    "symbols": [],
    "architecture": ["content lives under src/content"],
    "patterns": ["use exported functions"],
    "constraints": [],
    "assumptions": [],
    "commands": {"build": "npm run build"},
}
RESULT = {
    "changed": ["added the tags field"],
    "decisions": ["tags are optional"],
    "interfaces": ["BlogPost.data.tags"],
    "verification": {
        "ran": ["npm run build"],
        "outcome": "passed",
        "detail": "clean",
    },
    "notes_for_downstream": ["existing posts have no tags"],
}


def _item(key: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "title": f"Item {key}",
        "objective": "do the thing",
        "scope": "just the thing",
        "out_of_scope": "everything else",
        "dependencies": [],
        "files": ["src/x.ts"],
        "symbols": ["tags"],
        "write_scope": ["src/x.ts"],
        "acceptance_criteria": ["done"],
        "verification": [{"command_kind": "build", "reason": "compiles"}],
        "complexity": "low",
        "architecture": [],
        "risks": [],
    }
    item.update(overrides)
    return item


def _add_context(
    store: ControllerStore,
    context_id: str,
    *,
    command: str = "npm run build",
    architecture: str = "content lives under src/content",
) -> None:
    store.start_implementation_context(
        {
            "id": context_id,
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": ContextStatus.GENERATING.value,
            "provider": "claude",
            "model": "model",
        }
    )
    manifest = {**MANIFEST, "architecture": [architecture]}
    store.settle_implementation_context(
        context_id,
        to_status=ContextStatus.READY.value,
        changes={
            "manifest_json": json.dumps(manifest),
            "commands_json": json.dumps(
                [
                    {
                        "kind": "build",
                        "command": command,
                        "confirmed": True,
                        "reason": "defined",
                    }
                ]
            ),
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> ControllerStore:
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    register_ready_v1_sandbox(
        controller_store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="sample-volume",
        created_at="2026-08-08T00:00:00Z",
    )
    controller_store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Add tags",
        status=PlanningStatus.PLAN_READY.value,
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    controller_store.set_plan_spec(session_id="session-1", plan_spec=PLAN)
    _add_context(controller_store, "context-1")
    return controller_store


class _Tasks:
    def __init__(self, store: ControllerStore) -> None:
        self.store = store
        self.counter = 0
        self.prompts: list[str] = []
        self.settings: list[CodingTurnSettings] = []
        self.accepted: list[str] = []
        self.rejected: list[str] = []
        self.committed = True
        self.turn_status = "succeeded"
        self.payload: dict[str, Any] | None = dict(RESULT)
        self.reject_error = ""
        self.run_count = 0
        self.verifications: list[dict[str, Any]] = []

    def start_task(self, _client: Any, _store: ControllerStore, request: Any) -> Task:
        self.counter += 1
        task_id = f"{self.counter:032x}"
        self.store.create_task(
            task_id=task_id,
            sandbox_id="sandbox-1",
            agent_run_id=None,
            branch=f"task/{task_id}",
            base_branch="main",
            base_commit="a" * 40,
            title=request.title,
            status=TaskStatus.OPEN.value,
        )
        return Task.model_validate(self.store.task(task_id))

    def run_task(
        self,
        _client: Any,
        _store: ControllerStore,
        settings: CodingTurnSettings,
        task_id: str,
        request: Any,
    ) -> TaskRunResponse:
        self.prompts.append(request.prompt)
        self.settings.append(settings)
        self.run_count += 1
        if self.committed:
            self.store.advance_task_status(
                task_id=task_id,
                from_statuses=[TaskStatus.OPEN.value],
                to_status=TaskStatus.REPORTED.value,
                head_commit=f"{self.run_count + 10:x}" * 40,
            )
        return TaskRunResponse(
            task=Task.model_validate(self.store.task(task_id)),
            turn_status=self.turn_status,
            turn_error=None if self.committed else "nothing committed",
            committed=self.committed,
            detail="branch verified" if self.committed else "no commit",
            model=request.model or settings.model(request.provider.value),
            usage=TurnUsageView(input_tokens=11, output_tokens=22, cost_usd=0.05),
            duration_ms=1234,
            exit_code=0,
            tool_calls=1,
            result=self.payload,
        )

    def accept_task(
        self,
        _client: Any,
        _store: ControllerStore,
        task_id: str,
    ) -> Task:
        self.accepted.append(task_id)
        self.store.advance_task_status(
            task_id=task_id,
            from_statuses=[TaskStatus.REVIEW.value],
            to_status=TaskStatus.ACCEPTED.value,
            settled=True,
        )
        return Task.model_validate(self.store.task(task_id))

    def run_verification(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if self.verifications:
            return self.verifications.pop(0)
        return {
            "passed": True,
            "commands": [
                {
                    "command_kind": "build",
                    "command": "npm run build",
                    "passed": True,
                    "detail": "Passed",
                    "output": "",
                }
            ],
        }

    def reject_task(
        self,
        _client: Any,
        _store: ControllerStore,
        task_id: str,
    ) -> Task:
        if self.reject_error:
            raise TaskOperationError(409, self.reject_error)
        self.rejected.append(task_id)
        self.store.advance_task_status(
            task_id=task_id,
            from_statuses=[
                TaskStatus.OPEN.value,
                TaskStatus.REPORTED.value,
                TaskStatus.REVIEW.value,
            ],
            to_status=TaskStatus.REJECTED.value,
            settled=True,
        )
        return Task.model_validate(self.store.task(task_id))


@pytest.fixture
def tasks(store: ControllerStore, monkeypatch: pytest.MonkeyPatch) -> _Tasks:
    stub = _Tasks(store)
    monkeypatch.setattr(execution, "start_task", stub.start_task)
    monkeypatch.setattr(execution, "run_task", stub.run_task)
    monkeypatch.setattr(execution, "accept_task", stub.accept_task)
    monkeypatch.setattr(execution, "reject_task", stub.reject_task)
    monkeypatch.setattr(execution, "run_verification", stub.run_verification)
    return stub


def _delegation(store: ControllerStore, items: list[dict[str, Any]]) -> Any:
    return service.create_revision(store, "session-1", items, project_name="sample")


def _start(store: ControllerStore, delegation_id: str, key: str) -> Any:
    return execution.start_run(
        object(),
        SETTINGS,
        store,
        delegation_id,
        key,
        StartRunRequest(),
        session_id="session-1",
        project_name="sample",
    )


def test_verified_run_merges_and_releases_dependencies_automatically(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(
        store,
        [_item("a"), _item("b", dependencies=["a"])],
    )

    outcome = _start(store, delegation.delegation.id, "a")

    assert outcome.committed is True
    assert outcome.task_status == TaskStatus.ACCEPTED.value
    assert outcome.run_status is RunStatus.SUCCEEDED
    by_key = {entry.item.key: entry for entry in outcome.delegation.items}
    assert by_key["a"].state is WorkItemState.COMPLETED
    assert by_key["b"].state is WorkItemState.READY


def test_accept_releases_dependency_and_records_metrics(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(
        store,
        [_item("a"), _item("b", dependencies=["a"])],
    )
    started = _start(store, delegation.delegation.id, "a")

    run = started.delegation.items[0].runs[0]
    assert tasks.accepted == [started.task_id]
    assert started.run_status is RunStatus.SUCCEEDED
    assert run.usage.input_tokens == 11
    assert run.usage.cost_usd == 0.05
    assert run.duration_ms == 1234
    assert run.exit_code == 0
    assert run.result == RESULT
    assert started.delegation.items[1].state is WorkItemState.READY


def test_upstream_result_reaches_next_packet(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(
        store,
        [_item("a"), _item("b", dependencies=["a"])],
    )
    _start(store, delegation.delegation.id, "a")

    packet = execution.build_run_packet(store, delegation.delegation.id, "b")

    assert packet.upstream[0].key == "a"
    assert packet.upstream[0].interfaces == ["BlogPost.data.tags"]
    assert packet.upstream[0].notes == ["existing posts have no tags"]


def test_packet_uses_the_context_the_delegation_recorded(
    store: ControllerStore,
) -> None:
    """The packet quotes the pinned context_id, not whatever is newest.

    With one context per session those are the same row today. The pin still
    carries the guarantee, and `claim_context` refuses to overwrite the row
    while a delegation exists.
    """
    delegation = _delegation(store, [_item("a")])

    packet = execution.build_run_packet(store, delegation.delegation.id, "a")

    assert packet.verification[0].command == "npm run build"
    assert "content lives under src/content" in packet.architecture


def test_blocked_item_cannot_start(store: ControllerStore, tasks: _Tasks) -> None:
    delegation = _delegation(
        store,
        [_item("a"), _item("b", dependencies=["a"])],
    )

    with pytest.raises(service.DelegationOperationError) as error:
        _start(store, delegation.delegation.id, "b")

    assert error.value.status_code == 409
    assert "blocked by ['a']" in error.value.detail


def test_active_run_keeps_its_busy_message(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegation = _delegation(store, [_item("a")])

    def raise_busy(_values: dict[str, Any]) -> int:
        raise RunActive(delegation.delegation.id)

    monkeypatch.setattr(store, "claim_work_item_run", raise_busy)
    with pytest.raises(service.DelegationOperationError) as error:
        _start(store, delegation.delegation.id, "a")

    assert error.value.status_code == 409
    assert error.value.detail == "Another work item run is already active"
    assert tasks.rejected


def test_failed_turn_is_classified_cleaned_up_and_retryable(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    tasks.committed = False
    tasks.turn_status = "succeeded"
    delegation = _delegation(store, [_item("a")])

    first = _start(store, delegation.delegation.id, "a")

    assert first.run_status is RunStatus.FAILED
    assert first.task_id in tasks.rejected
    run = first.delegation.items[0].runs[0]
    assert run.failure_kind is FailureKind.IMPLEMENTATION
    assert run.usage.cost_usd == 0.05
    assert first.delegation.items[0].state is WorkItemState.FAILED

    tasks.committed = True
    tasks.turn_status = "succeeded"
    second = _start(store, delegation.delegation.id, "a")
    assert second.run_status is RunStatus.SUCCEEDED
    assert [run.attempt for run in second.delegation.items[0].runs] == [1, 2]


def test_cleanup_failure_halts_delegation(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    tasks.committed = False
    tasks.reject_error = "worktree is dirty"
    delegation = _delegation(store, [_item("a")])

    outcome = _start(store, delegation.delegation.id, "a")

    assert outcome.run_status is RunStatus.FAILED
    assert outcome.delegation.delegation.status is DelegationStatus.HALTED
    assert "worktree is dirty" in (outcome.delegation.delegation.error or "")


def test_provider_failure_retries_once_then_halts(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    tasks.committed = False
    tasks.turn_status = "provider_failure"
    delegation = _delegation(store, [_item("a")])

    outcome = _start(store, delegation.delegation.id, "a")

    assert tasks.run_count == 2
    assert outcome.run_status is RunStatus.FAILED
    assert outcome.delegation.delegation.status is DelegationStatus.HALTED
    assert outcome.delegation.items[0].runs[0].usage.cost_usd == 0.1


def test_provider_retry_can_recover(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegation = _delegation(store, [_item("a")])
    original = tasks.run_task

    def flaky(*args: Any, **kwargs: Any) -> TaskRunResponse:
        if tasks.run_count == 0:
            tasks.committed = False
            tasks.turn_status = "provider_failure"
        else:
            tasks.committed = True
            tasks.turn_status = "succeeded"
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "run_task", flaky)

    outcome = _start(store, delegation.delegation.id, "a")

    assert tasks.run_count == 2
    assert outcome.run_status is RunStatus.SUCCEEDED
    assert outcome.task_status == TaskStatus.ACCEPTED.value


def test_missing_sandbox_cleans_up_the_task(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegation = _delegation(store, [_item("a")])
    monkeypatch.setattr(store, "sandbox", lambda _sandbox_id: None)

    with pytest.raises(service.DelegationOperationError) as error:
        _start(store, delegation.delegation.id, "a")

    assert error.value.status_code == 404
    assert error.value.detail == "Delegation sandbox was not found"
    assert tasks.rejected
    task = Task.model_validate(store.task(tasks.rejected[0]))
    assert task.status is TaskStatus.REJECTED
    run = service.view(store, delegation.delegation.id).items[0].runs[0]
    assert run.status is RunStatus.FAILED


def test_verification_failure_gets_one_focused_repair(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    tasks.verifications = [
        {
            "passed": False,
            "commands": [
                {
                    "command": "npm run build",
                    "passed": False,
                    "detail": "Exited with status 1",
                    "output": "type error",
                }
            ],
        },
        {"passed": True, "commands": []},
    ]
    delegation = _delegation(store, [_item("a")])

    outcome = _start(store, delegation.delegation.id, "a")

    run = outcome.delegation.items[0].runs[0]
    assert outcome.run_status is RunStatus.SUCCEEDED
    assert run.repair_count == 1
    assert run.verification and run.verification["passed"] is True
    assert "Focused repair" in tasks.prompts[1]
    assert "type error" in tasks.prompts[1]


def test_repeated_verification_failure_halts(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    failed = {
        "passed": False,
        "commands": [
            {
                "command": "npm run build",
                "passed": False,
                "detail": "Exited with status 1",
                "output": "still broken",
            }
        ],
    }
    tasks.verifications = [failed, failed]
    delegation = _delegation(store, [_item("a")])

    outcome = _start(store, delegation.delegation.id, "a")

    run = outcome.delegation.items[0].runs[0]
    assert outcome.run_status is RunStatus.FAILED
    assert outcome.delegation.delegation.status is DelegationStatus.HALTED
    assert run.failure_kind is FailureKind.VERIFICATION
    assert run.repair_count == 1
    assert run.verification and run.verification["passed"] is False


def test_missing_result_is_reported_without_failing_commit(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    tasks.payload = None
    delegation = _delegation(store, [_item("a")])

    outcome = _start(store, delegation.delegation.id, "a")

    assert outcome.run_status is RunStatus.SUCCEEDED
    assert outcome.result_errors == ["the run reported no JSON result object"]


def test_completed_item_cannot_be_rejected_individually(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(
        store,
        [_item("a"), _item("b", dependencies=["a"])],
    )
    started = _start(store, delegation.delegation.id, "a")

    with pytest.raises(service.DelegationOperationError) as error:
        execution.reject_run(
            object(),
            store,
            delegation.delegation.id,
            started.run_id,
            "not good enough",
        )

    assert error.value.status_code == 409
    assert "succeeded" in error.value.detail
    assert tasks.rejected == []


def test_accepting_final_item_completes_delegation(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(store, [_item("a")])
    started = _start(store, delegation.delegation.id, "a")

    outcome = started

    assert outcome.delegation.delegation.status is DelegationStatus.COMPLETED
    assert outcome.delegation.delegation.settled_at is not None


def test_run_uses_requested_model_and_credential_profile(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(store, [_item("a")])

    outcome = execution.start_run(
        object(),
        SETTINGS,
        store,
        delegation.delegation.id,
        "a",
        StartRunRequest(model="chosen-model", credential_profile="work"),
    )

    assert outcome.model == "chosen-model"
    assert outcome.routing_source == "run_preference"
    assert tasks.settings[0].credential_profile == "work"
    assert "do the thing" in tasks.prompts[0]
    assert "npm run build" in tasks.prompts[0]
    assert "# Full plan" not in tasks.prompts[0]


def test_item_routing_override_wins_run_preference(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(store, [_item("a", complexity="high")])
    item = delegation.items[0].item
    store.set_work_item_routing(
        item.id,
        provider="claude",
        model="small-model",
        actor="human",
    )

    outcome = execution.start_run(
        object(),
        SETTINGS,
        store,
        delegation.delegation.id,
        "a",
        StartRunRequest(model="run-model"),
    )

    assert outcome.model == "small-model"
    assert outcome.routing_source == "item_override"
    assert outcome.routing_warning is None


def test_project_and_session_scope_hide_packets_and_runs(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(store, [_item("a")])
    started = _start(store, delegation.delegation.id, "a")

    with pytest.raises(service.DelegationOperationError) as packet_error:
        execution.build_run_packet(
            store,
            delegation.delegation.id,
            "a",
            project_name="other",
        )
    with pytest.raises(service.DelegationOperationError) as run_error:
        execution.accept_run(
            object(),
            store,
            delegation.delegation.id,
            started.run_id,
            session_id="other",
        )

    assert packet_error.value.status_code == 404
    assert run_error.value.status_code == 404
