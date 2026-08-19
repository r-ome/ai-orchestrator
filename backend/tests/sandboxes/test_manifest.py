from dataclasses import replace
from pathlib import Path

import pytest

from app.controller.store import ControllerStore
from app.sandboxes.manifest import (
    SandboxManifest,
    read_manifest,
    transition_sandbox_lifecycle,
    write_manifest,
)
from app.sandboxes.models import SandboxLifecycleStatus


def _store_with_sandbox(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_v1_project(
        project_id="project-1",
        remote_url="https://example.test/project-1.git",
        default_branch="main",
        mirror_volume="project-1-mirror",
        created_at="2026-08-11T00:00:00Z",
    )
    store.register_v1_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="sample-volume",
        created_at="2026-08-11T00:00:00Z",
    )
    return store


def test_created_base_commit_is_write_once(tmp_path: Path) -> None:
    store = _store_with_sandbox(tmp_path)
    manifest = SandboxManifest(
        sandbox_id="sandbox-1",
        lifecycle_version="v1",
        feature_key="add-manifest",
        created_base_commit="commit-one",
    )
    write_manifest(store, manifest)

    write_manifest(store, manifest)
    with pytest.raises(ValueError, match="created_base_commit.*immutable"):
        write_manifest(store, replace(manifest, created_base_commit="commit-two"))


def test_illegal_lifecycle_transition_changes_nothing(tmp_path: Path) -> None:
    store = _store_with_sandbox(tmp_path)
    manifest = SandboxManifest(
        sandbox_id="sandbox-1",
        lifecycle_version="v1",
        feature_key="guard-lifecycle",
        desired_state="active",
        lifecycle_status=SandboxLifecycleStatus.CREATING,
        operation="create",
    )
    assert write_manifest(store, manifest)

    assert not transition_sandbox_lifecycle(
        store,
        replace(manifest, operation="publish"),
        to_status=SandboxLifecycleStatus.PUBLISHING,
    )
    stored = read_manifest(store, manifest.sandbox_id)
    assert stored is not None
    assert stored.lifecycle_status is SandboxLifecycleStatus.CREATING
    assert stored.operation == "create"


def test_same_status_lifecycle_transition_changes_nothing(tmp_path: Path) -> None:
    store = _store_with_sandbox(tmp_path)
    manifest = SandboxManifest(
        sandbox_id="sandbox-1",
        lifecycle_version="v1",
        feature_key="guard-self-transition",
        desired_state="active",
        lifecycle_status=SandboxLifecycleStatus.CREATING,
        operation="create",
        operation_phase="workspace",
    )
    assert write_manifest(store, manifest)

    assert not transition_sandbox_lifecycle(
        store,
        replace(
            manifest,
            operation="resume",
            operation_phase="database_provisioning",
            last_error="must not persist",
        ),
        to_status=SandboxLifecycleStatus.CREATING,
    )
    assert read_manifest(store, manifest.sandbox_id) == manifest


def test_v1_manifest_requires_a_human_feature_key(tmp_path: Path) -> None:
    store = _store_with_sandbox(tmp_path)

    with pytest.raises(ValueError, match="feature_key is required"):
        write_manifest(
            store,
            SandboxManifest(sandbox_id="sandbox-1", lifecycle_version="v1"),
        )
