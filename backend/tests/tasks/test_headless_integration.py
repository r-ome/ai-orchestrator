"""A headless coding turn end to end, against real git and a real model.

Throwaway repository in its own volume, so no real sandbox is touched. Proves
the path a delegated work item will take: open a task, run a turn with nobody
at the terminal, verify the branch, reach review without a preview, accept.
Set HEADLESS_TEST_PROVIDER to select Claude or Codex.
"""

import json
import os
import subprocess
from pathlib import Path

import docker
import pytest
from conftest import register_ready_v1_sandbox

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.controller.store.task_status import TaskStatus
from app.projects.models import ProjectRegistration
from app.tasks import service as task_service
from app.tasks.config import CodingTurnSettings
from app.tasks.models import RunTaskRequest, StartTaskRequest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_HEADLESS_TESTS") != "1",
    reason="set RUN_DOCKER_HEADLESS_TESTS=1 to use the local Docker daemon and a real model",
)

SOURCE = Path("/Users/jeromeagapay/.orchestrator-headless-tests")
VOLUME = "orchestrator-headless-tests"
PROVIDER = AgentProvider(os.getenv("HEADLESS_TEST_PROVIDER", "claude"))
GIT_IMAGE = f"orchestrator-agent-{PROVIDER.value}:latest"
DEFAULT_MODEL = (
    "claude-haiku-4-5-20251001" if PROVIDER is AgentProvider.CLAUDE else "gpt-5.6-terra"
)
MODEL = os.getenv("HEADLESS_TEST_MODEL", DEFAULT_MODEL)

SETTINGS = CodingTurnSettings(
    timeout_seconds=900,
    memory="4g",
    max_log_bytes=2_000_000,
    claude_model=MODEL if PROVIDER is AgentProvider.CLAUDE else "claude-sonnet-5",
    codex_model=MODEL if PROVIDER is AgentProvider.CODEX else "gpt-5.6-terra",
    codex_reasoning_effort="medium",
    credential_profile="default",
)

PROMPT = """You are implementing one unit of work in a larger feature.

## What this must do

Add a `multiply(a, b)` function to src/math.js that returns a * b, exported as
a named export alongside the existing `add`.

## In scope

src/math.js only.

## Out of scope

src/helpers.js. Another unit owns it. Leave it alone.

## How this codebase does things

- CommonJS: module.exports and require, matching the existing files.
- One function per operation, no classes.

## Done when

- src/math.js exports multiply(a, b) returning a * b
- the existing add export is unchanged
"""


def _read(script: str) -> str:
    return subprocess.run(
        f"docker run --rm -v {VOLUME}:/w:ro -w /w {GIT_IMAGE} sh -c "
        f"'git config --global --add safe.directory /w >/dev/null 2>&1; {script}'",
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    subprocess.run(f"rm -rf {SOURCE}", shell=True, check=True)
    (SOURCE / "src").mkdir(parents=True)
    (SOURCE / "src" / "math.js").write_text(
        "function add(a, b) {\n  return a + b;\n}\n\nmodule.exports = { add };\n"
    )
    (SOURCE / "src" / "helpers.js").write_text(
        "const { add } = require('./math');\n\n"
        "function double(n) {\n  return add(n, n);\n}\n\n"
        "module.exports = { double };\n"
    )
    (SOURCE / "package.json").write_text(json.dumps({"name": "scratch", "scripts": {}}))
    for command in (
        "git init -q -b main",
        "git -c user.name=P -c user.email=p@e.com add -A",
        "git -c user.name=P -c user.email=p@e.com commit -qm initial",
    ):
        subprocess.run(command, shell=True, cwd=SOURCE, check=True)

    client = docker.from_env()
    subprocess.run(
        f"docker volume rm -f {VOLUME}", shell=True, capture_output=True, check=False
    )
    client.volumes.create(name=VOLUME)
    subprocess.run(
        f'docker run --rm -v "{SOURCE}":/source:ro -v {VOLUME}:/project alpine:3 '
        'sh -c "tar -C /source -cf - . | tar -C /project -xf -"',
        shell=True,
        check=True,
        capture_output=True,
    )

    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="scratch",
        source_path="managed:project-1",
        volume_name=VOLUME,
        created_at="2026-08-08T00:00:00Z",
        ready=True,
    )
    register_ready_v1_sandbox(
        store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
    )
    monkeypatch.setattr(
        task_service,
        "inspect_registered_project",
        lambda *_a, **_k: project,
    )
    try:
        yield store, client
    finally:
        subprocess.run(
            f"docker volume rm -f {VOLUME}",
            shell=True,
            capture_output=True,
            check=False,
        )
        subprocess.run(f"rm -rf {SOURCE}", shell=True, check=False)
        client.close()


def test_a_headless_turn_commits_verifies_and_accepts(sandbox) -> None:
    store, client = sandbox

    task = task_service.start_task(
        client,
        store,
        StartTaskRequest(project_name="scratch", title="Add a multiply operation"),
    )
    print(f"\ntask {task.id} on {task.branch} from {task.base_commit[:8]}")
    assert task.status is TaskStatus.OPEN
    assert _read("git rev-parse --abbrev-ref HEAD") == task.branch

    response = task_service.run_task(
        client,
        store,
        SETTINGS,
        task.id,
        RunTaskRequest(prompt=PROMPT, provider=PROVIDER, model=MODEL),
    )

    print(f"turn:      {response.turn_status}  committed={response.committed}")
    print(f"detail:    {response.detail}")
    print(f"model:     {response.model}")
    print(f"cost:      ${response.usage.cost_usd}")
    print(
        f"tokens:    in={response.usage.input_tokens} out={response.usage.output_tokens}"
    )
    print(f"duration:  {response.duration_ms} ms")
    print(f"tools:     {response.tool_calls} ({response.failed_tool_calls} failed)")
    print(f"result:    {response.result}")

    assert response.turn_status == "succeeded", response.turn_error
    assert response.committed is True
    assert response.task.status is TaskStatus.REPORTED
    assert response.failed_tool_calls == 0
    # Cost and the model actually used are recorded, which is what makes the
    # per-run measurement in the brief possible.
    if PROVIDER is AgentProvider.CLAUDE:
        assert response.usage.cost_usd is not None
    else:
        # Codex JSONL reports tokens but does not report a dollar amount.
        assert response.usage.input_tokens is not None
    assert response.model

    # The work is on the branch and nowhere else yet.
    assert "multiply" in _read(f"git show {task.branch}:src/math.js")
    assert "multiply" not in _read("git show main:src/math.js")
    # Scope was respected.
    assert _read(f"git diff --name-only main..{task.branch}") == "src/math.js"

    verified = task_service.verify_task(store, task.id)
    print(f"verified:  {verified.status.value}")
    assert verified.status is TaskStatus.REVIEW

    accepted = task_service.accept_task(client, store, task.id)
    print(f"accepted:  {accepted.status.value}")
    assert accepted.status is TaskStatus.ACCEPTED

    merged = _read("git show main:src/math.js")
    assert "multiply" in merged
    assert "function add" in merged
    print(f"\nmerged src/math.js:\n{merged}")
