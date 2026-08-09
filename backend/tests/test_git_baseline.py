import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import docker
import pytest
from docker.errors import NotFound

from app.agents.config import AgentSettings
from app.agents.models import AgentProvider, CreateAgentRequest
from app.agents.service import create_agent, stop_agent
from app.controller.store import ControllerStore
from app.projects.service import ensure_git_baseline


requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)

GIT_IMAGE = "alpine/git:latest"


@requires_docker
def test_folder_without_git_becomes_a_repository_with_a_baseline_commit() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    volume = client.volumes.create(name=f"orchestrator-git-baseline-test-{run_id[:12]}")
    try:
        client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["printf 'hello' > /project/index.html"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "rw"}},
        )

        baseline_commit = ensure_git_baseline(client, GIT_IMAGE, volume.name)

        assert len(baseline_commit) == 40

        log = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["cd /project && git log --format=%s"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
        )
        assert log.decode().strip() == "sandbox baseline"
    finally:
        volume.remove(force=True)


@requires_docker
def test_folder_already_a_git_repository_keeps_its_history() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    volume = client.volumes.create(name=f"orchestrator-git-baseline-test-{run_id[:12]}")
    try:
        existing_commit = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "cd /project\n"
                "git init -q -b main\n"
                'git config user.name "agent"\n'
                'git config user.email "agent@localhost"\n'
                "printf 'work' > work.txt\n"
                "git add -A\n"
                'git commit -q -m "agent work"\n'
                "git rev-parse HEAD\n"
            ],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "rw"}},
        )
        existing_commit = existing_commit.decode().strip()

        baseline_commit = ensure_git_baseline(client, GIT_IMAGE, volume.name)

        assert baseline_commit == existing_commit

        log = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["cd /project && git log --format=%s"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
        )
        assert log.decode().strip() == "agent work"
    finally:
        volume.remove(force=True)


def test_store_migration_adds_baseline_commit_and_runs_twice_without_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "controller.sqlite3"

    # Simulate a pre-Phase-0 database: a sandboxes table without
    # baseline_commit, already past migration versions 1 and 2.
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE sandboxes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                project_name TEXT NOT NULL,
                volume_name TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            """
            INSERT INTO projects(id, source_path, created_at)
            VALUES ('project-1', '/projects/sample', '2026-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO sandboxes(
                id, project_id, project_name, volume_name, status, created_at, updated_at
            ) VALUES (
                'sandbox-1', 'project-1', 'sample-sandbox-1', 'orchestrator-project-sample-1',
                'ready', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = ControllerStore(database_path)
    store.initialize()
    store.initialize()

    store.set_sandbox_baseline_commit(
        sandbox_id="sandbox-1",
        baseline_commit="a" * 40,
    )

    sandboxes = {row["id"]: row for row in store.sandboxes()}
    assert sandboxes["sandbox-1"]["baseline_commit"] == "a" * 40

    connection = sqlite3.connect(database_path)
    try:
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    finally:
        connection.close()
    # Subset, not equality: later phases add further migration versions.
    assert {1, 2, 3} <= versions


def test_store_migration_is_idempotent_on_a_fresh_database(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.initialize()

    store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        source_path="/projects/sample",
        volume_name="orchestrator-project-sample-1",
        status="ready",
        created_at="2026-01-01T00:00:00Z",
    )
    store.set_sandbox_baseline_commit(
        sandbox_id="sandbox-1",
        baseline_commit="b" * 40,
    )

    sandboxes = {row["id"]: row for row in store.sandboxes()}
    assert sandboxes["sandbox-1"]["baseline_commit"] == "b" * 40


# --- create_agent gives an on-demand baseline to sandboxes that lack one ---


class _StubVolume:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.attrs = {"Name": name, "Labels": labels}


class _StubVolumes:
    def __init__(self) -> None:
        self.items: dict[str, _StubVolume] = {}

    def get(self, name: str) -> _StubVolume:
        try:
            return self.items[name]
        except KeyError as error:
            raise NotFound("volume not found") from error

    def create(self, **kwargs: Any) -> _StubVolume:
        volume = _StubVolume(kwargs["name"], kwargs["labels"])
        self.items[volume.name] = volume
        return volume


class _StubContainer:
    def __init__(self, create_args: dict[str, Any], number: int) -> None:
        self.id = f"agent-container-{number:04d}"
        self.short_id = self.id[:12]
        self.name = create_args["name"]
        self.status = "created"
        self.attrs = {
            "Created": f"2026-08-06T10:00:{number:02d}Z",
            "Config": {
                "Image": create_args["image"],
                "Labels": create_args["labels"],
            },
        }

    def start(self) -> None:
        self.status = "running"

    def stop(self, *, timeout: int) -> None:
        self.status = "exited"

    def remove(self, *, force: bool) -> None:
        self.status = "removed"


class _StubContainers:
    def __init__(self) -> None:
        self.items: list[_StubContainer] = []

    def create(self, **kwargs: Any) -> _StubContainer:
        container = _StubContainer(kwargs, len(self.items) + 1)
        self.items.append(container)
        return container

    def run(self, **kwargs: Any) -> bytes:
        # Used by _volume_runtime_files when resolving the agent's
        # dependency volume; an empty sandbox has no lockfile to find.
        return b""

    def list(self, **kwargs: Any) -> list[_StubContainer]:
        return [container for container in self.items if container.status != "removed"]

    def get(self, agent_id: str) -> _StubContainer:
        for container in self.items:
            if agent_id in {container.id, container.short_id, container.name}:
                return container
        raise NotFound("container not found")


class _StubDockerClient:
    def __init__(self) -> None:
        self.volumes = _StubVolumes()
        self.containers = _StubContainers()


def test_agent_creation_gives_a_baseline_to_a_sandbox_missing_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = _StubDockerClient()
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    project = SimpleNamespace(
        name="Sample Project",
        volume_name="orchestrator-project-sample",
        ready=True,
    )
    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        lambda *_: project,
    )
    baseline_calls: list[str] = []

    def fake_ensure_git_baseline(
        _docker_client: Any, _git_image: str, volume_name: str
    ) -> str:
        baseline_calls.append(volume_name)
        return "c" * 40

    monkeypatch.setattr(
        "app.agents.service.ensure_git_baseline", fake_ensure_git_baseline
    )
    settings = AgentSettings(
        claude_image="test-claude:latest",
        codex_image="test-codex:latest",
    )

    agent = create_agent(
        docker_client,
        settings,
        CreateAgentRequest(project_name="Sample Project", provider=AgentProvider.CLAUDE),
        controller_store,
    )

    assert baseline_calls == ["orchestrator-project-sample"]
    assert controller_store.sandbox_baseline_commit(agent.sandbox_id) == "c" * 40

    stop_agent(docker_client, agent.id, controller_store=controller_store)
    create_agent(
        docker_client,
        settings,
        CreateAgentRequest(project_name="Sample Project", provider=AgentProvider.CLAUDE),
        controller_store,
    )

    # A second agent creation on the same sandbox reuses the recorded
    # baseline instead of spawning another git container.
    assert baseline_calls == ["orchestrator-project-sample"]


@requires_docker
def test_controller_scaffolding_is_excluded_from_the_sandbox_repository() -> None:
    """`.agent/` and friends are the sandbox's, not the project's.

    They are created inside the volume by the controller and its agents. Left
    visible to git they read as uncommitted work, which made settlement refuse
    every task in an imported repository. The rule goes in `.git/info/exclude`
    rather than `.gitignore` so it never reaches the project's own files.
    """
    client = docker.from_env()
    run_id = uuid4().hex
    volume = client.volumes.create(name=f"orchestrator-git-baseline-test-{run_id[:12]}")
    try:
        client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "cd /project\n"
                "git init -q -b main\n"
                'git config user.name "agent"\n'
                'git config user.email "agent@localhost"\n'
                "printf 'work' > work.txt\n"
                "git add -A\n"
                'git commit -q -m "project history"\n'
                # Pre-existing local excludes must survive.
                "mkdir -p .git/info\n"
                "printf '.env\\n' >> .git/info/exclude\n"
                # Scaffolding the controller writes, plus real untracked content.
                "mkdir -p .agent .claude .orchestrator notes\n"
                "printf 'x' > .claude/settings.json\n"
                "printf 'x' > notes/todo.md\n"
            ],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "rw"}},
        )

        ensure_git_baseline(client, GIT_IMAGE, volume.name)
        # Twice: the sandbox is re-baselined on every task, so appending has to
        # be idempotent or the exclude file grows without bound.
        ensure_git_baseline(client, GIT_IMAGE, volume.name)

        status = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["cd /project && git status --porcelain"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
        )
        exclude = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["cd /project && cat .git/info/exclude"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
        )
    finally:
        volume.remove(force=True)

    # The project's own untracked content stays visible; scaffolding does not.
    assert status.decode().split() == ["??", "notes/"]
    lines = exclude.decode().splitlines()
    assert ".env" in lines
    assert lines.count("/.agent/") == 1
    assert lines.count("/.claude/") == 1
    assert lines.count("/.orchestrator/") == 1
