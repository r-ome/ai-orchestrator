import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import register_ready_v1_sandbox

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.controller.store.delegation_status import DelegationStatus
from app.implementation_context import service
from app.implementation_context.config import ContextSettings
from app.implementation_context.inventory import parse_inventory
from app.implementation_context.models import ContextStatus, GenerateContextRequest
from app.implementation_context.validators import validate_context_payload
from app.planning.config import PlanningSettings
from app.planning.models import PlanningStatus
from app.planning.runner import PlanningTurnError, TurnResult

CONTEXT_SETTINGS = ContextSettings(
    model="claude-sonnet-5",
    git_image="orchestrator-agent-claude:latest",
    inventory_timeout_seconds=60,
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
PACKAGE = {
    "package.json": json.dumps({"scripts": {"build": "astro build", "test": "vitest"}})
}
PNPM_PROJECT = {
    "package.json": json.dumps(
        {
            "scripts": {"build": "astro build", "test": "vitest"},
            "dependencies": {"astro": "^4.5.0"},
        }
    ),
    "pnpm-lock.yaml": "",
    ".github/workflows/ci.yml": (
        "jobs:\n  check:\n    steps:\n"
        "      - run: pnpm install --frozen-lockfile\n"
        "      - run: pnpm run test -- --coverage\n"
    ),
}
PLAN = {
    "title": "Add reading time",
    "scope": "Show reading time on posts",
    "approach": "Compute it from the body",
    "components": [{"name": "post page", "responsibility": "renders it"}],
    "risks": [],
    "open_questions": [],
    "reviewer_outcome": {"approved": True, "rounds": 1},
    "plan_markdown": "# Plan",
    "confirmed_understanding": True,
    "generated_at": "2026-08-08T00:00:00Z",
}


def _manifest(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "modules": [{"path": "src/pages/blog.astro", "purpose": "renders a post"}],
        "symbols": [
            {"name": "formatDate", "location": "src/utils.ts", "role": "byline"}
        ],
        "architecture": ["content lives under src/content"],
        "patterns": ["utilities live in src/utils"],
        "constraints": ["Astro 5"],
        "assumptions": ["posts are markdown"],
        "commands": {"test": "npm test", "build": "npm run build"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControllerStore:
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    register_ready_v1_sandbox(
        controller_store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="orchestrator-project-sample-sandbox-1",
        created_at="2026-08-08T00:00:00Z",
    )
    _create_session(controller_store)
    monkeypatch.setattr(
        service,
        "discover_inventory",
        lambda *_args, **_kwargs: parse_inventory(PACKAGE),
    )
    return controller_store


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
        title="Add reading time",
        status=PlanningStatus.CLARIFYING.value,
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="profile-1",
        max_review_turns=3,
    )
    store.set_plan_spec(session_id=session_id, plan_spec=PLAN)
    if status is PlanningStatus.CLARIFYING:
        return
    store.advance_planning_status(
        session_id=session_id,
        from_statuses=[PlanningStatus.CLARIFYING.value],
        to_status=PlanningStatus.PLANNING.value,
    )
    store.advance_planning_status(
        session_id=session_id,
        from_statuses=[PlanningStatus.PLANNING.value],
        to_status=PlanningStatus.UNDER_REVIEW.value,
    )
    store.advance_planning_status(
        session_id=session_id,
        from_statuses=[PlanningStatus.UNDER_REVIEW.value],
        to_status=status.value,
        settled=True,
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
        queued = self.queue.pop(0) if self.queue else _result(_manifest())
        if isinstance(queued, Exception):
            raise queued
        return queued


def _result(payload: dict[str, Any]) -> TurnResult:
    return TurnResult(
        raw_output=json.dumps(payload), payload=payload, model="claude-sonnet-5"
    )


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> _Turns:
    stub = _Turns()
    monkeypatch.setattr(service, "run_planning_turn", stub)
    return stub


def _generate(
    store: ControllerStore,
    session_id: str = "session-1",
    request: GenerateContextRequest | None = None,
    *,
    project_name: str | None = None,
) -> Any:
    return service.generate_context(
        object(),
        PLANNING_SETTINGS,
        CONTEXT_SETTINGS,
        store,
        session_id,
        request or GenerateContextRequest(),
        project_name=project_name,
    )


def test_valid_manifest_settles_ready_with_confirmed_commands(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    turns.queue.append(_result(_manifest()))

    outcome = _generate(store)

    assert outcome.accepted
    assert outcome.context.status is ContextStatus.READY
    assert outcome.context.settled_at is not None
    assert outcome.unconfirmed_commands == []
    assert outcome.context.confirmed_commands == {
        "test": "npm test",
        "build": "npm run build",
    }
    assert outcome.context.manifest is not None
    assert outcome.context.manifest.modules[0].path == "src/pages/blog.astro"


def test_prompt_names_confirmed_commands_and_uses_session_credentials(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _generate(store)

    assert "npm run build" in turns.prompts[0]
    assert "npm run test" in turns.prompts[0]
    assert turns.settings[0].credential_profile == "profile-1"
    assert turns.settings[0].claude_model == "claude-sonnet-5"


def test_prompt_carries_the_package_manager_ci_and_pinned_versions(
    store: ControllerStore,
    turns: _Turns,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evidence that replaces guessing.

    Read from the repository before the turn starts, so the turn cannot spend
    tool calls on it or misread it.
    """
    monkeypatch.setattr(
        service,
        "discover_inventory",
        lambda *_args, **_kwargs: parse_inventory(PNPM_PROJECT),
    )

    _generate(store)
    prompt = turns.prompts[0]

    assert "package manager is pnpm" in prompt
    assert "pnpm run build" in prompt
    # Word boundary: "npm run build" is a substring of "pnpm run build".
    assert re.search(r"\bnpm run build", prompt) is None
    assert "pnpm run test -- --coverage" in prompt
    assert "astro ^4.5.0" in prompt


def test_requested_codex_model_is_applied_to_the_turn(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _generate(
        store,
        request=GenerateContextRequest(provider=AgentProvider.CODEX, model="gpt-test"),
    )

    assert turns.settings[0].codex_model == "gpt-test"


def test_invented_command_is_repaired_once(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    turns.queue.extend(
        [
            _result(_manifest(commands={"test": "npm run test:unit"})),
            _result(_manifest(commands={"test": "npm test"})),
        ]
    )

    outcome = _generate(store)

    assert outcome.accepted
    assert outcome.attempts == 2
    assert "test:unit" in turns.prompts[1]
    assert outcome.unconfirmed_commands == []


def test_unconfirmed_command_after_repair_keeps_context(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    bad = _result(_manifest(commands={"test": "npm run nope"}))
    turns.queue.extend([bad, bad])

    outcome = _generate(store)

    assert outcome.accepted
    assert outcome.context.status is ContextStatus.READY
    assert [command.kind for command in outcome.unconfirmed_commands] == ["test"]
    assert outcome.context.confirmed_commands == {}


def test_structurally_invalid_manifest_fails_context(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    broken = _result({"modules": [], "commands": {}})
    turns.queue.extend([broken, broken])

    outcome = _generate(store)

    assert not outcome.accepted
    assert outcome.context.status is ContextStatus.FAILED
    assert outcome.context.error


def test_invalid_json_output_fails_context_after_repair(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    failure = PlanningTurnError(422, "No JSON object found", "not json")
    turns.queue.extend([failure, failure])

    outcome = _generate(store)

    assert not outcome.accepted
    assert outcome.context.status is ContextStatus.FAILED
    assert outcome.turn_status == "invalid_output"


def test_regenerating_replaces_the_one_context_in_place(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    first = _generate(store)
    second = _generate(store)

    # Same row, same id: there is nothing to choose between, and a delegation
    # that recorded this context_id earlier still resolves.
    assert second.context.id == first.context.id
    current = service.session_context(store, "session-1")
    assert current is not None
    assert current.id == first.context.id
    assert service.ready_context(store, "session-1") is not None


def test_delegation_freezes_the_context_against_regeneration(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    """The guard that replaces revisions.

    With one context row per session, regenerating would overwrite what a
    delegation's packets were built from. So it stops being writable once a
    delegation exists, and becomes writable again if that delegation is
    abandoned.
    """
    first = _generate(store)
    store.claim_delegation_revision(
        {
            "id": "delegation-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "context_id": first.context.id,
            "status": DelegationStatus.READY.value,
        },
        [],
    )

    with pytest.raises(service.ContextOperationError) as error:
        _generate(store)

    assert error.value.status_code == 409
    assert "abandon the delegation" in error.value.detail.lower()

    store.transition_delegation(
        "delegation-1",
        to_status=DelegationStatus.ABANDONED.value,
        from_statuses=(DelegationStatus.READY.value,),
        terminal=True,
    )
    assert _generate(store).context.id == first.context.id


def test_review_limit_session_can_generate_context(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    _create_session(
        store,
        session_id="session-limited",
        status=PlanningStatus.REVIEW_LIMIT_REACHED,
    )

    outcome = _generate(store, "session-limited")

    assert outcome.accepted


def test_context_needs_completed_plan(store: ControllerStore, turns: _Turns) -> None:
    _create_session(store, session_id="session-open", status=PlanningStatus.CLARIFYING)

    with pytest.raises(service.ContextOperationError) as error:
        _generate(store, "session-open")

    assert error.value.status_code == 409


def test_project_scope_hides_session_and_context(
    store: ControllerStore,
    turns: _Turns,
) -> None:
    outcome = _generate(store, project_name="sample")

    with pytest.raises(service.ContextOperationError) as session_error:
        service.session_context(store, "session-1", project_name="other")
    with pytest.raises(service.ContextOperationError) as context_error:
        service.get_context(
            store,
            outcome.context.id,
            session_id="session-1",
            project_name="other",
        )

    assert session_error.value.status_code == 404
    assert context_error.value.status_code == 404


def test_failed_turn_does_not_leave_context_generating(
    store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> TurnResult:
        raise RuntimeError("docker exploded")

    monkeypatch.setattr(service, "run_planning_turn", _boom)

    with pytest.raises(RuntimeError):
        _generate(store)

    context = service.session_context(store, "session-1")
    assert context is not None
    assert context.status is ContextStatus.FAILED
    # A failed row does not block the next attempt: it is reset, not added to.
    assert service.ready_context(store, "session-1") is None


def test_manifest_validation_rejects_excerpts_unknown_commands_and_no_modules() -> None:
    excerpt_errors = validate_context_payload(_manifest(patterns=["x" * 2500]))
    command_errors = validate_context_payload(
        _manifest(commands={"deploy": "npm run deploy"})
    )

    assert any("record where to look" in error for error in excerpt_errors)
    assert any("unknown keys" in error for error in command_errors)
    assert validate_context_payload(_manifest(modules=[])) != []
