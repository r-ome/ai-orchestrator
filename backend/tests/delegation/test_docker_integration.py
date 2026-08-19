"""The Delegator against a real sandbox and model."""

import os
from pathlib import Path

import docker
import pytest
from conftest import register_ready_v1_sandbox

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.delegation import service
from app.delegation.config import DelegatorSettings
from app.delegation.models import GenerateDelegationRequest, WorkItemState
from app.implementation_context.config import ContextSettings
from app.implementation_context.models import GenerateContextRequest
from app.implementation_context.service import generate_context
from app.planning.config import PlanningSettings
from app.projects.service import inspect_registered_project, project_id

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_DELEGATOR_TESTS") != "1",
    reason="set RUN_DOCKER_DELEGATOR_TESTS=1 to use Docker and a real model",
)

PROJECT = os.getenv("DELEGATOR_TEST_PROJECT", "personal-blog-sandbox-1")
MODEL = os.getenv("DELEGATOR_TEST_MODEL", "claude-sonnet-5")
GIT_IMAGE = "orchestrator-agent-claude:latest"
CONTEXT_SETTINGS = ContextSettings(
    model=MODEL,
    git_image=GIT_IMAGE,
    inventory_timeout_seconds=60,
)
DELEGATOR_SETTINGS = DelegatorSettings(
    model=MODEL,
)
PLANNING_SETTINGS = PlanningSettings(
    clarifier_provider=AgentProvider.CLAUDE,
    planner_provider=AgentProvider.CLAUDE,
    reviewer_provider=AgentProvider.CODEX,
    credential_profile="default",
    max_review_turns=3,
    turn_timeout_seconds=600,
    planning_memory="2g",
    claude_model=MODEL,
    codex_model="gpt-5.6-terra",
    codex_reasoning_effort="high",
)
PLAN = {
    "title": "Add reading time",
    "scope": "Show reading time under each blog post title.",
    "approach": "Compute it from the body and render it beside the byline.",
    "components": [
        {"name": "reading-time utility", "responsibility": "computes minutes"},
        {"name": "post page", "responsibility": "renders reading time"},
    ],
    "risks": [{"severity": "medium", "text": "Code blocks affect word counts"}],
    "open_questions": [],
    "reviewer_outcome": {"approved": True, "rounds": 1},
    "plan_markdown": "# Plan\n\nAdd and render reading time.",
    "confirmed_understanding": True,
    "generated_at": "2026-08-08T00:00:00Z",
}


@pytest.fixture
def context(tmp_path: Path):
    client = docker.from_env()
    project = inspect_registered_project(client, PROJECT)
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id=project.sandbox_id,
        project_id=project_id(project.source_path),
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
    )
    store.create_planning_session(
        session_id="session-1",
        project_id=project_id(project.source_path),
        sandbox_id=project.sandbox_id,
        project_name=project.name,
        title="Add reading time",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.set_plan_spec(session_id="session-1", plan_spec=PLAN)
    try:
        yield store, client, project
    finally:
        client.close()


def test_delegator_produces_valid_startable_graph(context) -> None:
    store, client, project = context
    generated = generate_context(
        client,
        PLANNING_SETTINGS,
        CONTEXT_SETTINGS,
        store,
        "session-1",
        GenerateContextRequest(model=MODEL),
        project_name=project.name,
    )
    assert generated.accepted, generated.validation_errors

    outcome = service.generate_revision(
        client,
        PLANNING_SETTINGS,
        DELEGATOR_SETTINGS,
        store,
        "session-1",
        GenerateDelegationRequest(model=MODEL),
        project_name=project.name,
    )

    assert outcome.accepted, outcome.validation_errors
    assert outcome.delegation is not None
    assert outcome.delegation.ready
    assert outcome.delegation.waves[0]
    keys = {entry.item.key for entry in outcome.delegation.items}
    for entry in outcome.delegation.items:
        assert entry.item.acceptance_criteria
        assert entry.item.verification
        assert set(entry.item.dependencies) <= keys
        if entry.wave == 0:
            assert entry.state is WorkItemState.READY
