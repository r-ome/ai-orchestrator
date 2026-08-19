"""Phase 1: the dependency volume is keyed by sandbox and lockfile digest.

Covers ADR 0003 (controller-owned dependency volumes): a dependency volume
name is stable for an unchanged lockfile, changes when the lockfile changes,
tolerates a missing lockfile, and is always mounted read-only into the
coding agent.
"""

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import NotFound

from app.agents.config import AgentSettings
from app.agents.models import AgentProvider, CreateAgentRequest
from app.agents.service import create_agent
from app.controller.store import ControllerStore
from app.previews.dependency_cache import (
    _dependency_volume,
    _dependency_volume_name,
    _dependency_volume_ready,
    _lockfile_digest,
)
from app.previews.errors import PreviewOperationError
from app.projects.models import ProjectRegistration
from conftest import register_ready_v1_sandbox


# --- _lockfile_digest -------------------------------------------------------


def test_identical_lockfiles_produce_the_same_digest() -> None:
    files_a = {"package-lock.json": b'{"lockfileVersion": 3}'}
    files_b = {"package-lock.json": b'{"lockfileVersion": 3}'}

    assert _lockfile_digest(files_a) == _lockfile_digest(files_b)


def test_a_changed_lockfile_produces_a_different_digest() -> None:
    before = _lockfile_digest({"package-lock.json": b'{"lockfileVersion": 3}'})
    after = _lockfile_digest({"package-lock.json": b'{"lockfileVersion": 4}'})

    assert before != after


def test_missing_lockfile_digests_the_literal_string_none() -> None:
    assert _lockfile_digest({}) == hashlib.sha256(b"none").hexdigest()
    assert _lockfile_digest({"README.md": b"docs"}) == hashlib.sha256(b"none").hexdigest()


def test_lockfile_priority_prefers_package_lock_over_pnpm_lock() -> None:
    files = {
        "package-lock.json": b"npm-lock",
        "pnpm-lock.yaml": b"pnpm-lock",
    }

    assert _lockfile_digest(files) == _lockfile_digest({"package-lock.json": b"npm-lock"})


def test_lockfile_digest_ignores_nested_lockfiles_outside_the_volume_root() -> None:
    # Only a lockfile at the sandbox volume root keys the dependency volume.
    files = {"vendor/package-lock.json": b"nested"}

    assert _lockfile_digest(files) == hashlib.sha256(b"none").hexdigest()


# --- _dependency_volume_name -------------------------------------------------


def test_dependency_volume_name_is_deterministic() -> None:
    assert _dependency_volume_name("sandbox-1", "digest-1") == _dependency_volume_name(
        "sandbox-1", "digest-1"
    )


def test_dependency_volume_name_changes_with_the_digest() -> None:
    assert _dependency_volume_name("sandbox-1", "aaaa") != _dependency_volume_name(
        "sandbox-1", "bbbb"
    )


def test_dependency_volume_name_changes_with_the_sandbox() -> None:
    assert _dependency_volume_name("sandbox-1", "aaaa") != _dependency_volume_name(
        "sandbox-2", "aaaa"
    )


def test_dependency_volume_name_truncates_sandbox_and_digest_to_twelve_characters() -> None:
    name = _dependency_volume_name("s" * 40, "d" * 40)

    assert name == f"orchestrator-deps-{'s' * 12}-{'d' * 12}"


# --- Docker stubs -------------------------------------------------------


class StubVolume:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.attrs = {"Name": name, "Labels": labels}


class StubVolumes:
    def __init__(self) -> None:
        self.items: dict[str, StubVolume] = {}
        self.create_calls: list[dict[str, Any]] = []

    def get(self, name: str) -> StubVolume:
        try:
            return self.items[name]
        except KeyError as error:
            raise NotFound("volume not found") from error

    def create(self, **kwargs: Any) -> StubVolume:
        self.create_calls.append(kwargs)
        volume = StubVolume(kwargs["name"], kwargs["labels"])
        self.items[volume.name] = volume
        return volume


class StubContainer:
    def __init__(self, create_args: dict[str, Any], number: int) -> None:
        self.id = f"agent-container-{number:04d}"
        self.short_id = self.id[:12]
        self.name = create_args.get("name", f"agent-helper-{number}")
        self.status = "created"
        self.attrs = {
            "Created": "2026-08-06T00:00:00Z",
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
        del stderr
        return self.log_output if stdout else b""

    def remove(self, *, force: bool) -> None:
        del force
        self.status = "removed"


class StubContainers:
    def __init__(self, lockfile_tar: bytes = b"") -> None:
        self.items: list[StubContainer] = []
        self.create_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self._lockfile_tar = lockfile_tar

    def create(self, **kwargs: Any) -> StubContainer:
        self.create_calls.append(kwargs)
        container = StubContainer(kwargs, len(self.items) + 1)
        container.log_output = base64.b64encode(self._lockfile_tar)
        self.items.append(container)
        return container

    def run(self, **kwargs: Any) -> bytes:
        # Stands in for the inspection container _volume_runtime_files runs;
        # an empty sandbox volume has no lockfile to find.
        self.run_calls.append(kwargs)
        return self._lockfile_tar

    def list(self, **kwargs: Any) -> list[StubContainer]:
        return []


class StubDockerClient:
    def __init__(self, lockfile_tar: bytes = b"") -> None:
        self.volumes = StubVolumes()
        self.containers = StubContainers(lockfile_tar)


# --- _dependency_volume -------------------------------------------------


def test_dependency_volume_reuses_an_existing_volume_by_name() -> None:
    docker_client = StubDockerClient()
    labels = {"orchestrator.sandbox.id": "sandbox-1"}

    created = _dependency_volume(docker_client, "sandbox-1", "digest-1", labels)
    reused = _dependency_volume(docker_client, "sandbox-1", "digest-1", labels)

    assert created.name == reused.name
    assert len(docker_client.volumes.create_calls) == 1


def test_dependency_volume_is_labeled_like_a_persistent_data_volume() -> None:
    docker_client = StubDockerClient()
    labels = {"orchestrator.sandbox.id": "sandbox-1"}

    volume = _dependency_volume(docker_client, "sandbox-1", "digest-1", labels)

    assert volume.attrs["Labels"]["orchestrator.preview.data-managed"] == "true"
    assert volume.attrs["Labels"]["orchestrator.preview.persistent"] == "true"
    assert volume.attrs["Labels"]["orchestrator.sandbox.id"] == "sandbox-1"


def test_dependency_volume_rejects_a_volume_it_does_not_trust() -> None:
    docker_client = StubDockerClient()
    name = _dependency_volume_name("sandbox-1", "digest-1")
    docker_client.volumes.items[name] = StubVolume(name, {"untrusted": "true"})

    with pytest.raises(PreviewOperationError):
        _dependency_volume(docker_client, "sandbox-1", "digest-1", {})


def test_dependency_volume_is_reused_only_after_install_completion() -> None:
    settings = SimpleNamespace(inspection_image="inspection:latest")
    incomplete = StubDockerClient(lockfile_tar=b"")
    complete = StubDockerClient(lockfile_tar=b"ready")

    assert not _dependency_volume_ready(incomplete, settings, "dependencies")
    assert _dependency_volume_ready(complete, settings, "dependencies")
    command = complete.containers.create_calls[0]["command"][-1]
    assert ".orchestrator-install-complete" in command


# --- create_agent mounts the dependency volume read-only -------------------


SETTINGS = AgentSettings(
    claude_image="test-claude:latest",
    codex_image="test-codex:latest",
)


def _ready_project() -> ProjectRegistration:
    return ProjectRegistration(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="managed:project-1",
        volume_name="orchestrator-project-sample",
        created_at="2026-08-06T00:00:00Z",
        ready=True,
    )


def _register_ready_sandbox(store: ControllerStore) -> None:
    project = _ready_project()
    register_ready_v1_sandbox(
        store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
        db_engine="none",
    )


def test_agent_mounts_the_dependency_volume_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    _register_ready_sandbox(controller_store)
    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        lambda *_: _ready_project(),
    )
    monkeypatch.setattr(
        "app.agents.service.ensure_git_baseline",
        lambda *_: "a" * 40,
    )

    create_agent(
        docker_client,
        SETTINGS,
        CreateAgentRequest(project_name="Sample Project", provider=AgentProvider.CLAUDE),
        controller_store,
    )

    create_call = next(
        call
        for call in docker_client.containers.create_calls
        if str(call.get("name", "")).startswith("orchestrator-agent-")
    )
    dependency_mounts = {
        name: mount
        for name, mount in create_call["volumes"].items()
        if name.startswith("orchestrator-deps-")
    }
    assert len(dependency_mounts) == 1
    (mount,) = dependency_mounts.values()
    assert mount == {"bind": "/workspace/node_modules", "mode": "ro"}


def test_agent_creates_an_empty_dependency_volume_when_no_preview_has_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = StubDockerClient()
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    _register_ready_sandbox(controller_store)
    monkeypatch.setattr(
        "app.agents.service.inspect_registered_project",
        lambda *_: _ready_project(),
    )
    monkeypatch.setattr(
        "app.agents.service.ensure_git_baseline",
        lambda *_: "a" * 40,
    )

    agent = create_agent(
        docker_client,
        SETTINGS,
        CreateAgentRequest(project_name="Sample Project", provider=AgentProvider.CLAUDE),
        controller_store,
    )

    assert agent.id
    dependency_calls = [
        call
        for call in docker_client.volumes.create_calls
        if call["name"].startswith("orchestrator-deps-")
    ]
    assert len(dependency_calls) == 1
