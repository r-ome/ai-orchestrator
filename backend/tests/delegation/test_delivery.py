import base64
import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import docker
import pytest

from app.controller.store import ControllerStore
from app.delegation import delivery, service
from app.delegation.models import (
    ChangeRequestStatus,
    DelegationStatus,
    FeatureDiff,
    IntegrationReview,
    IntegrationReviewStatus,
    RunStatus,
)
from app.dirty_state import DirtyEntry, serialize_snapshot
from app.previews.config import PreviewSettings
from app.sandboxes import router as sandbox_router
from app.sandboxes import service as sandbox_service
from app.sandboxes.engine_detection import EngineDetection, NO_DATABASE
from app.sandboxes.manifest import read_manifest, transition_sandbox_lifecycle
from app.sandboxes.models import SandboxLifecycleStatus
from app.sandboxes.naming import ownership_labels, workspace_volume
from conftest import register_ready_v1_sandbox


BASE = "1" * 40
MIDDLE = "2" * 40
HEAD = "3" * 40
NOW = "2026-08-09T00:00:00Z"
PREVIEW_SETTINGS = PreviewSettings(
    inspection_image="inspect",
    default_expiry_minutes=30,
    maximum_file_bytes=1_000,
    maximum_snapshot_bytes=10_000,
    proposal_lifetime_seconds=900,
    prepare_timeout_seconds=600,
    build_timeout_seconds=900,
    git_image="alpine/git:latest",
)
requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


class _Containers:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> "_Container":
        self.calls.append(kwargs)
        return _Container(self.outputs.pop(0))


class _Container:
    def __init__(self, output: bytes) -> None:
        self.output = output

    def start(self) -> None:
        pass

    def wait(self, *, timeout: int) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        return self.output if stdout else b""

    def remove(self, *, force: bool) -> None:
        pass


class _Docker:
    def __init__(self, *outputs: bytes) -> None:
        self.containers = _Containers(list(outputs))


def _review(**overrides: Any) -> IntegrationReview:
    values: dict[str, Any] = {
        "id": "review-1",
        "delegation_id": "delegation-1",
        "revision": 1,
        "status": "completed",
        "provider": "claude",
        "model": "model",
        "base_branch": "main",
        "base_commit": BASE,
        "head_commit": HEAD,
        "approved": True,
        "summary": "approved",
        "findings": [],
        "error": None,
        "created_at": NOW,
        "updated_at": NOW,
        "settled_at": NOW,
        "source_merged_at": None,
    }
    values.update(overrides)
    return IntegrationReview.model_validate(values)


def _view(review: IntegrationReview | None = None) -> Any:
    return SimpleNamespace(
        delegation=SimpleNamespace(
            id="delegation-1",
            sandbox_id="sandbox-1",
            status=DelegationStatus.COMPLETED,
        ),
        items=[],
        changes=[],
        review=review,
    )


def _state(*entries: DirtyEntry, branch: str = "main", head: str = HEAD) -> bytes:
    lines = [f"branch {branch}", f"head {head}", "snapshot-version 1"]
    for entry in entries:
        status = base64.b64encode(entry.status.encode()).decode()
        path = base64.b64encode(entry.path.encode()).decode()
        fingerprint = entry.fingerprint or "-"
        lines.append(
            f"snapshot {status} {entry.file_type} {fingerprint} {path}"
        )
    return ("\n".join(lines) + "\n").encode()


def _untracked(path: str, fingerprint: str = "sha256:original") -> DirtyEntry:
    return DirtyEntry(path, "??", "file", fingerprint)


class _DirtyStore:
    def __init__(
        self,
        baseline: list[DirtyEntry] | None,
        *,
        legacy_paths: list[str] | None = None,
        source_path: str = "/projects/sample",
    ) -> None:
        # Mirrors a real `sandboxes` row: it carries project_id, never
        # source_path. That column lives on `projects`.
        self.sandbox_row: dict[str, Any] = {
            "id": "sandbox-1",
            "project_id": "project-1",
            "volume_name": "sandbox-volume",
            "dirty_baseline_json": (
                serialize_snapshot(baseline) if baseline is not None else None
            ),
        }
        self.project_row: dict[str, Any] = {
            "id": "project-1",
            "source_path": source_path,
        }
        self.legacy_paths = legacy_paths

    def sandbox(self, _sandbox_id: str) -> dict[str, Any]:
        return self.sandbox_row

    def project(self, _project_id: str) -> dict[str, Any]:
        return self.project_row

    def task(self, _task_id: str) -> dict[str, Any]:
        return {
            "status": "accepted",
            "base_branch": "main",
            "base_commit": BASE,
            "head_commit": HEAD,
        }

    def tasks_for_sandbox(self, _sandbox_id: str) -> list[dict[str, Any]]:
        if self.legacy_paths is None:
            return []
        return [{"baseline_dirty_json": json.dumps(self.legacy_paths)}]

    def set_sandbox_dirty_baseline_if_missing(
        self,
        *,
        sandbox_id: str,
        baseline_json: str,
    ) -> bool:
        assert sandbox_id == "sandbox-1"
        if self.sandbox_row["dirty_baseline_json"] is not None:
            return False
        self.sandbox_row["dirty_baseline_json"] = baseline_json
        return True


def _one_task_view(review: IntegrationReview | None = None) -> Any:
    view = _view(review)
    view.items = [
        SimpleNamespace(
            item=SimpleNamespace(key="item"),
            runs=[SimpleNamespace(status=RunStatus.SUCCEEDED, task_id="task-1")],
        )
    ]
    return view


def test_capture_feature_target_follows_the_accepted_task_chain() -> None:
    tasks = {
        "task-a": {
            "status": "accepted",
            "base_branch": "main",
            "base_commit": BASE,
            "head_commit": MIDDLE,
        },
        "task-b": {
            "status": "accepted",
            "base_branch": "main",
            "base_commit": MIDDLE,
            "head_commit": HEAD,
        },
    }
    store = SimpleNamespace(
        task=lambda task_id: tasks.get(task_id),
        sandbox=lambda _sandbox_id: {"volume_name": "sandbox-volume"},
    )
    view = _view()
    view.items = [
        SimpleNamespace(
            item=SimpleNamespace(key="a"),
            runs=[SimpleNamespace(status=RunStatus.SUCCEEDED, task_id="task-a")],
        ),
        SimpleNamespace(
            item=SimpleNamespace(key="b"),
            runs=[SimpleNamespace(status=RunStatus.SUCCEEDED, task_id="task-b")],
        ),
    ]
    docker = _Docker(f"branch main\nhead {HEAD}\ndirty false\n".encode())

    target = delivery.capture_feature_target(
        docker,
        PREVIEW_SETTINGS,
        store,
        view,
    )

    assert target == delivery.FeatureTarget("main", BASE, HEAD)
    assert docker.containers.calls[0]["network_mode"] == "none"
    assert docker.containers.calls[0]["volumes"]["sandbox-volume"]["mode"] == "ro"


def test_unchanged_pre_existing_untracked_file_allows_feature_review() -> None:
    position = _untracked("interview-prep/Position.md")

    target = delivery.capture_feature_target(
        _Docker(_state(position)),
        PREVIEW_SETTINGS,
        _DirtyStore([position]),
        _one_task_view(),
    )

    assert target == delivery.FeatureTarget("main", BASE, HEAD)


def test_modified_pre_existing_file_blocks_feature_review() -> None:
    position = _untracked("interview-prep/Position.md")

    with pytest.raises(service.DelegationOperationError) as error:
        delivery.capture_feature_target(
            _Docker(_state(_untracked(position.path, "sha256:changed"))),
            PREVIEW_SETTINGS,
            _DirtyStore([position]),
            _one_task_view(),
        )

    assert error.value.status_code == 409
    assert position.path in error.value.detail
    assert "content modified" in error.value.detail


def test_new_untracked_file_blocks_feature_review() -> None:
    added = _untracked("notes/new.txt")

    with pytest.raises(service.DelegationOperationError) as error:
        delivery.capture_feature_target(
            _Docker(_state(added)),
            PREVIEW_SETTINGS,
            _DirtyStore([]),
            _one_task_view(),
        )

    assert added.path in error.value.detail
    assert "new uncommitted file" in error.value.detail


def test_removed_pre_existing_file_blocks_feature_review() -> None:
    removed = _untracked("interview-prep/Position.md")

    with pytest.raises(service.DelegationOperationError) as error:
        delivery.capture_feature_target(
            _Docker(_state()),
            PREVIEW_SETTINGS,
            _DirtyStore([removed]),
            _one_task_view(),
        )

    assert removed.path in error.value.detail
    assert "removed pre-existing" in error.value.detail


def test_clean_sandbox_still_allows_feature_review() -> None:
    target = delivery.capture_feature_target(
        _Docker(_state()),
        PREVIEW_SETTINGS,
        _DirtyStore([]),
        _one_task_view(),
    )

    assert target.head_commit == HEAD


def test_dirty_error_identifies_every_blocking_path_and_change() -> None:
    modified = _untracked("interview-prep/Position.md")
    removed = _untracked("notes/old.txt")
    added = _untracked("notes/new.txt")

    with pytest.raises(service.DelegationOperationError) as error:
        delivery.capture_feature_target(
            _Docker(
                _state(
                    _untracked(modified.path, "sha256:changed"),
                    added,
                )
            ),
            PREVIEW_SETTINGS,
            _DirtyStore([modified, removed]),
            _one_task_view(),
        )

    assert modified.path in error.value.detail
    assert "content modified" in error.value.detail
    assert removed.path in error.value.detail
    assert "removed pre-existing" in error.value.detail
    assert added.path in error.value.detail
    assert "new uncommitted file" in error.value.detail


def test_legacy_directory_baseline_seeds_exact_fingerprints_once() -> None:
    position = _untracked("interview-prep/Position.md")
    store = _DirtyStore(None, legacy_paths=["interview-prep/"])

    delivery.capture_feature_target(
        _Docker(_state(position)),
        PREVIEW_SETTINGS,
        store,
        _one_task_view(),
    )

    assert store.sandbox_row["dirty_baseline_json"] == serialize_snapshot([position])


def test_feature_diff_returns_file_totals_and_a_unified_patch() -> None:
    numstat = b"5\t2\tsrc/app.py\n-\t-\tpublic/logo.png\n"
    patch = b"diff --git a/src/app.py b/src/app.py\n+new line\n"
    docker = _Docker(_state(), numstat, patch)
    store = SimpleNamespace(
        sandbox=lambda _sandbox_id: {
            "volume_name": "sandbox-volume",
            "project_id": "project-1",
        },
        project=lambda _project_id: {
            "id": "project-1",
            "source_path": "/projects/sample",
        },
    )

    result = delivery.feature_diff(
        docker,
        PREVIEW_SETTINGS,
        store,
        _view(_review()),
    )

    assert isinstance(result, FeatureDiff)
    assert result.review_id == "review-1"
    assert result.additions == 5
    assert result.deletions == 2
    assert result.files[1].binary is True
    assert "+new line" in result.patch


def test_feature_diff_checks_dirty_state_before_generating_the_patch() -> None:
    position = _untracked("interview-prep/Position.md")
    docker = _Docker(_state(_untracked(position.path, "sha256:changed")))

    with pytest.raises(service.DelegationOperationError) as error:
        delivery.feature_diff(
            docker,
            PREVIEW_SETTINGS,
            _DirtyStore([position]),
            _view(_review()),
        )

    assert position.path in error.value.detail
    assert len(docker.containers.calls) == 1


@pytest.mark.parametrize(
    "change_status",
    [ChangeRequestStatus.AWAITING_REVIEW, ChangeRequestStatus.COMPLETED],
)
def test_incorporated_change_invalidates_the_previous_review_target(
    change_status: ChangeRequestStatus,
) -> None:
    tasks = {
        "task-a": {
            "status": "accepted",
            "base_branch": "main",
            "base_commit": BASE,
            "head_commit": MIDDLE,
        },
        "task-change": {
            "status": "accepted",
            "base_branch": "main",
            "base_commit": MIDDLE,
            "head_commit": HEAD,
        },
    }
    store = SimpleNamespace(
        task=lambda task_id: tasks.get(task_id),
        sandbox=lambda _sandbox_id: {
            "volume_name": "sandbox-volume",
            "project_id": "project-1",
        },
        project=lambda _project_id: {
            "id": "project-1",
            "source_path": "/projects/sample",
        },
    )
    view = _view(_review(head_commit=MIDDLE))
    view.items = [
        SimpleNamespace(
            item=SimpleNamespace(key="a"),
            runs=[SimpleNamespace(status=RunStatus.SUCCEEDED, task_id="task-a")],
        )
    ]
    view.changes = [
        SimpleNamespace(
            revision=1,
            status=change_status,
            task_id="task-change",
            created_at="2026-08-10T00:00:00Z",
        )
    ]
    docker = _Docker(
        f"branch main\nhead {HEAD}\ndirty false\n".encode(),
        b"1\t0\tsrc/app.py\n",
        b"diff --git a/src/app.py b/src/app.py\n+refined\n",
    )

    result = delivery.feature_diff(docker, PREVIEW_SETTINGS, store, view)

    assert result.review_id is None
    assert result.head_commit == HEAD


class _MergeStore:
    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path

    def sandbox(self, _sandbox_id: str) -> dict[str, str]:
        return {"project_id": "project-1", "volume_name": "sandbox-volume"}

    def project(self, _project_id: str) -> dict[str, str]:
        return {"id": "project-1", "source_path": str(self.source_path)}

    def mark_delegation_review_source_merged(
        self,
        _review_id: str,
    ) -> dict[str, Any]:
        return {
            "id": "review-1",
            "delegation_id": "delegation-1",
            "revision": 1,
            "status": "completed",
            "provider": "claude",
            "model": "model",
            "base_branch": "main",
            "base_commit": BASE,
            "head_commit": HEAD,
            "result_json": json.dumps(
                {"approved": True, "summary": "approved", "findings": []}
            ),
            "error": None,
            "created_at": NOW,
            "updated_at": NOW,
            "settled_at": NOW,
            "source_merged_at": NOW,
        }


@requires_docker
def test_managed_v1_delivery_fast_forwards_its_feature_branch_and_drains_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise v1 branch delivery without starting an agent or model."""
    from fastapi import HTTPException

    from app.controller.store import get_controller_store

    client = docker.from_env()
    run_id = uuid4().hex[:12]
    sandbox_id = uuid4().hex
    project_id = uuid4().hex
    feature_key = f"delivery-{run_id}"
    branch = f"feature/{feature_key}"
    workspace = workspace_volume(sandbox_id)
    target = f"delivery-target-{run_id}"
    workspace_volume_handle = target_volume_handle = None

    def git(script: str, volumes: dict[str, dict[str, str]]) -> bytes:
        return client.containers.run(
            PREVIEW_SETTINGS.git_image,
            entrypoint=["sh", "-c"],
            command=[script],
            remove=True,
            volumes=volumes,
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )

    try:
        target_volume_handle = client.volumes.create(name=target)
        workspace_volume_handle = client.volumes.create(
            name=workspace,
            labels=ownership_labels(sandbox_id=sandbox_id, project_id=project_id),
        )
        base = git(
            "set -eu\n"
            f"git init -q -b {branch} /target\n"
            "git -C /target config user.name tester\n"
            "git -C /target config user.email tester@example.invalid\n"
            "printf base > /target/app.txt\n"
            "git -C /target add app.txt\n"
            "git -C /target commit -qm base\n"
            "git -C /target rev-parse HEAD\n",
            {target: {"bind": "/target", "mode": "rw"}},
        ).decode().strip()
        head = git(
            "set -eu\n"
            "git clone -q /target /workspace\n"
            "git -C /workspace remote remove origin\n"
            "git -C /workspace config user.name tester\n"
            "git -C /workspace config user.email tester@example.invalid\n"
            "printf feature >> /workspace/app.txt\n"
            "git -C /workspace add app.txt\n"
            "git -C /workspace commit -qm feature\n"
            "git -C /workspace rev-parse HEAD\n",
            {
                target: {"bind": "/target", "mode": "ro"},
                workspace: {"bind": "/workspace", "mode": "rw"},
            },
        ).decode().strip()

        store = get_controller_store()
        register_ready_v1_sandbox(
            store,
            sandbox_id=sandbox_id,
            project_id=project_id,
            project_name="delivery test",
            volume_name=workspace,
            remote_url=f"https://example.test/{run_id}.git",
            default_branch="main",
            mirror_volume=f"delivery-mirror-{run_id}",
            created_at="",
            feature_key=feature_key,
            desired_state="active",
            feature_branch=branch,
            base_ref="refs/heads/main",
            created_base_commit=base,
            current_base_commit=base,
            db_engine=NO_DATABASE,
        )
        store.record_sandbox_resource(sandbox_id, kind="volume", name=workspace)
        store.record_sandbox_engine_detection(
            sandbox_id=sandbox_id,
            signals=[],
            proposed_engine=NO_DATABASE,
            migrate_commands=[],
            seed_commands=[],
            commands_source={},
            detected_at_commit=base,
        )
        store.confirm_sandbox_engine_detection(
            sandbox_id=sandbox_id,
            engine=NO_DATABASE,
            migrate_commands=[],
            seed_commands=[],
            commands_source={},
            actor="tester",
        )
        store.create_planning_session(
            session_id="planning-1",
            project_id=project_id,
            sandbox_id=sandbox_id,
            project_name=sandbox_id,
            title="delivery",
            status="plan_ready",
            clarifier_provider="claude",
            planner_provider="claude",
            reviewer_provider="codex",
            credential_profile="default",
            max_review_turns=3,
        )
        store.claim_delegation_revision(
            {
                "id": "delegation-active",
                "session_id": "planning-1",
                "sandbox_id": sandbox_id,
                "context_id": None,
                "status": "ready",
            },
            [],
        )

        with pytest.raises(HTTPException) as blocked:
            sandbox_router.sync_sandbox(
                sandbox_id,
                sandbox_router.SyncSandboxRequest(),
                client,
                store,
            )
        assert blocked.value.status_code == 409
        assert blocked.value.detail["blocking_writer"] == {
            "class": "delegation",
            "id": "delegation-active",
        }

        assert store.transition_delegation(
            "delegation-active",
            to_status="completed",
            from_statuses=("ready",),
            terminal=True,
        ) is not None

        def complete_sync(*_args: object, **_kwargs: object) -> None:
            manifest = read_manifest(store, sandbox_id)
            assert manifest is not None
            assert transition_sandbox_lifecycle(
                store,
                replace(
                    manifest,
                    current_base_commit=manifest.pending_base_commit,
                    pending_base_commit=None,
                ),
                to_status=SandboxLifecycleStatus.READY,
            )

        monkeypatch.setattr(sandbox_service, "fetch_canonical_mirror", lambda *_a, **_k: None)
        monkeypatch.setattr(sandbox_service, "mirror_base_commit", lambda *_a, **_k: base)
        monkeypatch.setattr(sandbox_service, "sync_workspace_from_mirror", lambda *_a, **_k: None)
        monkeypatch.setattr(sandbox_service, "complete_database_provision", complete_sync)
        monkeypatch.setattr(
            sandbox_service,
            "discover_engine",
            lambda *_a, **_k: EngineDetection(
                signals=(),
                proposed_engine=NO_DATABASE,
                migrate_commands=(),
                seed_commands=(),
                commands_source={},
            ),
        )
        synced = sandbox_router.sync_sandbox(
            sandbox_id,
            sandbox_router.SyncSandboxRequest(),
            client,
            store,
        )
        assert synced.lifecycle_status == "ready"

        store.claim_delegation_revision(
            {
                "id": "delegation-drained",
                "session_id": "planning-1",
                "sandbox_id": sandbox_id,
                "context_id": None,
                "status": "ready",
            },
            [],
        )
        assert store.delegation("delegation-drained")["status"] == "ready"
        sandbox_router.delete_sandbox(sandbox_id, client, store)

        # Destroy drains writers before it deletes their rows: drain stops the
        # running work, the tombstone preserves the manifest, and the row itself
        # goes because delegations reference sandboxes without ON DELETE CASCADE.
        assert store.delegation("delegation-drained") is None
        assert client.volumes.list(filters={"name": f"sbx-{sandbox_id[:12]}"}) == []
        assert client.containers.list(
            all=True,
            filters={"name": f"sbx-{sandbox_id[:12]}"},
        ) == []
    finally:
        if workspace_volume_handle is not None:
            try:
                workspace_volume_handle.remove(force=True)
            except docker.errors.NotFound:
                pass
        if target_volume_handle is not None:
            try:
                target_volume_handle.remove(force=True)
            except docker.errors.NotFound:
                pass
        client.close()
