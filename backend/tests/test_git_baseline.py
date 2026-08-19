import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker
import pytest
from conftest import register_ready_v1_sandbox
from docker.errors import ContainerError, NotFound

from app.agents.config import AgentSettings
from app.agents.models import AgentProvider, CreateAgentRequest
from app.agents.service import create_agent, stop_agent
from app.controller.store import ControllerStore
from app.projects.models import ProjectRegistration
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
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )

        baseline_commit = ensure_git_baseline(client, GIT_IMAGE, volume.name)

        assert len(baseline_commit) == 40

        log = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["cd /project && git log --format=%s"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
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
            tmpfs={"/git": "rw,nosuid,size=1m"},
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
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        assert log.decode().strip() == "agent work"
    finally:
        volume.remove(force=True)


@requires_docker
def test_linked_worktree_or_submodule_gets_a_named_baseline_refusal() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    volume = client.volumes.create(name=f"orchestrator-git-baseline-test-{run_id[:12]}")
    try:
        client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["printf 'gitdir: /host/path/that-is-not-mounted\\n' > /project/.git"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )

        with pytest.raises(ContainerError) as raised:
            ensure_git_baseline(client, GIT_IMAGE, volume.name)

        # docker-py returns stderr as str or bytes depending on the path taken;
        # `describe_git_failure` handles both, so the test must too.
        stderr = raised.value.stderr
        output = stderr.decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        assert "linked worktree or submodule" in output
        assert "fatal: not a git repository" not in output
    finally:
        volume.remove(force=True)


@requires_docker
def test_git_baseline_does_not_run_a_project_pre_commit_hook() -> None:
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
                "mkdir -p .git/hooks\n"
                "printf '#!/bin/sh\\ntouch /project/hook-ran\\n' > .git/hooks/pre-commit\n"
                "chmod +x .git/hooks/pre-commit\n"
                "printf 'work' > work.txt\n"
            ],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )

        ensure_git_baseline(client, GIT_IMAGE, volume.name)

        marker = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["test ! -e /project/hook-ran && printf absent"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        assert marker == b"absent"
    finally:
        volume.remove(force=True)


def test_initial_migration_supports_git_baselines_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.initialize()

    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        volume_name="orchestrator-project-sample-1",
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
        self.name = create_args.get("name", f"agent-helper-{number}")
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

    def wait(self, *, timeout: int) -> dict[str, int]:
        del timeout
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        del stdout
        del stderr
        return b""

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
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="managed:project-1",
        volume_name="orchestrator-project-sample",
        created_at="2026-08-06T00:00:00Z",
        ready=True,
    )
    register_ready_v1_sandbox(
        controller_store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
        db_engine="none",
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


def test_agent_run_is_recorded_before_git_and_dependency_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = _StubDockerClient()
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="managed:project-1",
        volume_name="orchestrator-project-sample",
        created_at="2026-08-06T00:00:00Z",
        ready=True,
    )
    register_ready_v1_sandbox(
        controller_store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
        db_engine="none",
    )
    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        lambda *_: project,
    )
    order: list[str] = []

    def assert_claimed(step: str) -> None:
        active = controller_store.active_agents()
        assert len(active) == 1
        assert active[0]["status"] == "created"
        assert active[0]["container_id"] is None
        order.append(step)

    def baseline(*_: Any) -> str:
        assert_claimed("git")
        return "c" * 40

    def dependency(*_: Any, **__: Any) -> _StubVolume:
        assert_claimed("dependency")
        return _StubVolume("dependency-volume", {})

    monkeypatch.setattr("app.agents.service.ensure_git_baseline", baseline)
    monkeypatch.setattr("app.agents.service._agent_dependency_volume", dependency)

    create_agent(
        docker_client,
        AgentSettings(
            claude_image="test-claude:latest",
            codex_image="test-codex:latest",
        ),
        CreateAgentRequest(
            project_name="Sample Project",
            provider=AgentProvider.CLAUDE,
        ),
        controller_store,
    )

    assert order == ["git", "dependency"]


def test_agent_preparation_failure_marks_the_row_and_removes_its_container(
    tmp_path: Path,
    fake_docker_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from docker.errors import DockerException

    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="managed:project-1",
        volume_name="orchestrator-project-sample",
        created_at="2026-08-06T00:00:00Z",
        ready=True,
    )
    register_ready_v1_sandbox(
        controller_store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
        db_engine="none",
    )
    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        lambda *_: project,
    )
    monkeypatch.setattr(
        "app.agents.service.ensure_git_baseline",
        lambda *_: "c" * 40,
    )

    def dependency(*_: Any, **__: Any) -> Any:
        return fake_docker_client.volumes.create(
            name="dependency-volume",
            labels={},
        )

    monkeypatch.setattr("app.agents.service._agent_dependency_volume", dependency)
    fake_docker_client.inject_failure(
        "container.start",
        DockerException("start failed"),
    )

    with pytest.raises(DockerException, match="start failed"):
        create_agent(
            fake_docker_client,
            AgentSettings(
                claude_image="test-claude:latest",
                codex_image="test-codex:latest",
            ),
            CreateAgentRequest(
                project_name="Sample Project",
                provider=AgentProvider.CLAUDE,
            ),
            controller_store,
        )

    with controller_store._connection() as connection:
        rows = connection.execute("SELECT * FROM agent_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    agent_containers = [
        resource
        for resource in fake_docker_client.created
        if getattr(resource, "_kind", "") == "containers"
    ]
    assert len(agent_containers) == 1
    assert agent_containers[0].removed


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
            tmpfs={"/git": "rw,nosuid,size=1m"},
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
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        exclude = client.containers.run(
            GIT_IMAGE,
            entrypoint=["sh", "-c"],
            command=["cd /project && cat .git/info/exclude"],
            remove=True,
            volumes={volume.name: {"bind": "/project", "mode": "ro"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
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
