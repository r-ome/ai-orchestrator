from dataclasses import replace
from pathlib import Path

import pytest

from app.controller.store import ControllerStore
from app.sandboxes.manifest import SandboxManifest, read_manifest, write_manifest


def _store_with_sandbox(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        source_path="/projects/sample",
        volume_name="sample-volume",
        status="discovered",
        created_at="2026-08-11T00:00:00Z",
    )
    return store


def test_manifest_write_preserves_legacy_status(tmp_path: Path) -> None:
    store = _store_with_sandbox(tmp_path)
    manifest = SandboxManifest(
        sandbox_id="sandbox-1",
        lifecycle_version="v1",
        feature_key="add-manifest",
        desired_state="active",
        lifecycle_status="creating",
        feature_branch="feature/add-manifest",
    )

    write_manifest(store, manifest)

    sandbox = store.sandbox("sandbox-1")
    assert sandbox is not None
    assert sandbox["status"] == "discovered"
    assert read_manifest(store, "sandbox-1") == manifest


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


def test_v1_manifest_requires_a_human_feature_key(tmp_path: Path) -> None:
    store = _store_with_sandbox(tmp_path)

    with pytest.raises(ValueError, match="feature_key is required"):
        write_manifest(
            store,
            SandboxManifest(sandbox_id="sandbox-1", lifecycle_version="v1"),
        )
