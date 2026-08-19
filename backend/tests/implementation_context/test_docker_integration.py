"""Implementation context against a real local sandbox and model."""

import os
from pathlib import Path

import docker
import pytest

from conftest import register_ready_v1_sandbox

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.implementation_context.config import ContextSettings
from app.implementation_context.inventory import confirm_command, discover_inventory
from app.implementation_context.models import ContextStatus, GenerateContextRequest
from app.implementation_context.service import generate_context
from app.planning.config import PlanningSettings
from app.projects.service import inspect_registered_project, project_id


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_CONTEXT_TESTS") != "1",
    reason="set RUN_DOCKER_CONTEXT_TESTS=1 to use Docker and a real model",
)

PROJECT = os.getenv("CONTEXT_TEST_PROJECT", "personal-blog-sandbox-1")
GIT_IMAGE = "orchestrator-agent-claude:latest"
CONTEXT_SETTINGS = ContextSettings(
    model=os.getenv("CONTEXT_TEST_MODEL", "claude-sonnet-5"),
    git_image=GIT_IMAGE,
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
    claude_model=CONTEXT_SETTINGS.model,
    codex_model="gpt-5.6-terra",
    codex_reasoning_effort="high",
)
PLAN = {
    "title": "Add reading time",
    "scope": "Show an estimated reading time on each blog post.",
    "approach": "Compute reading time from the post body at render time.",
    "components": [
        {"name": "reading-time utility", "responsibility": "computes minutes"},
        {"name": "post detail page", "responsibility": "renders the value"},
    ],
    "risks": [{"severity": "medium", "text": "Code blocks affect word counts"}],
    "open_questions": [],
    "reviewer_outcome": {"approved": True, "rounds": 1},
    "plan_markdown": "# Plan\n\nAdd and render a reading-time utility.",
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


def test_inventory_reads_the_project_scripts(context) -> None:
    _store, client, project = context

    inventory = discover_inventory(
        client,
        image=GIT_IMAGE,
        volume_name=project.volume_name,
    )

    assert inventory.node_project
    assert inventory.npm_scripts
    for script in inventory.npm_scripts:
        assert confirm_command(f"npm run {script}", inventory)[0]
    assert not confirm_command("npm run definitely-not-a-script", inventory)[0]


def test_generated_confirmed_commands_exist_in_the_project(context) -> None:
    store, client, project = context
    inventory = discover_inventory(
        client,
        image=GIT_IMAGE,
        volume_name=project.volume_name,
    )

    outcome = generate_context(
        client,
        PLANNING_SETTINGS,
        CONTEXT_SETTINGS,
        store,
        "session-1",
        GenerateContextRequest(),
        project_name=project.name,
    )

    assert outcome.accepted, outcome.validation_errors
    assert outcome.context.status is ContextStatus.READY
    assert outcome.context.manifest is not None
    assert outcome.context.manifest.modules
    for command in outcome.context.commands:
        confirmed, reason = confirm_command(command.command, inventory)
        assert confirmed == command.confirmed, reason
