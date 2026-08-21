from pathlib import Path

import pytest

from app.controller.store import ControllerStore
from app.delegation.packet import ResolvedVerification
from app.delegation.verification import VerificationSettings, run_verification
from app.platform.naming import (
    database_name,
    db_data_volume,
    network,
    ownership_labels,
    workspace_volume,
)
from app.previews import resources as preview_resources
from app.previews.config import PreviewSettings
from app.previews.models import (
    PreviewConfiguration,
    PreviewKind,
    PreviewMode,
    PreviewNetworkAccess,
    PreviewRuntime,
)
from app.previews.runtimes import native as preview_native
from app.sandboxes.manifest import SandboxManifest, write_manifest


def _managed_database(
    store: ControllerStore,
    docker_client,
    *,
    sandbox_id: str,
    project_id: str,
    engine: str = "sqlite",
) -> None:
    store.register_v1_project(
        project_id=project_id,
        remote_url="https://github.com/owner/repo.git",
        default_branch="main",
        mirror_volume=f"prj-{project_id[:12]}-mirror",
        created_at="2026-08-11T00:00:00Z",
    )
    store.register_v1_sandbox(
        sandbox_id=sandbox_id,
        project_id=project_id,
        project_name="https://github.com/owner/repo.git",
        volume_name=workspace_volume(sandbox_id),
        created_at="2026-08-11T00:00:00Z",
    )
    write_manifest(
        store,
        SandboxManifest(
            sandbox_id=sandbox_id,
            lifecycle_version="v1",
            feature_key="database-consumer",
            desired_state="active",
            lifecycle_status="ready",
            db_engine=engine,
            db_name=database_name(sandbox_id),
            db_data_volume=db_data_volume(sandbox_id) if engine == "sqlite" else None,
            schema_baseline_hash="baseline",
        ),
    )
    store.ensure_sandbox_database(
        sandbox_id=sandbox_id,
        engine=engine,
        db_name=database_name(sandbox_id),
        username=database_name(sandbox_id),
        password="sandbox-only",
    )
    store.update_sandbox_database_status(sandbox_id, status="ready", provisioned=True)
    labels = ownership_labels(sandbox_id=sandbox_id, project_id=project_id)
    docker_client.volumes.create(name=workspace_volume(sandbox_id), labels=labels)
    if engine == "sqlite":
        docker_client.volumes.create(name=db_data_volume(sandbox_id), labels=labels)
    docker_client.networks.create(network(sandbox_id), labels=labels, internal=True)


def test_task_archive_preview_mounts_the_same_sandbox_sqlite_database(
    tmp_path: Path,
    fake_docker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    sandbox_id = "a" * 32
    project_id = "b" * 32
    _managed_database(
        store, fake_docker_client, sandbox_id=sandbox_id, project_id=project_id
    )
    calls: list[dict[str, object]] = []
    exports: list[tuple[str, str, str]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    def run_network(docker_client, run_id, labels, access):
        created = docker_client.networks.create(
            f"orchestrator-preview-{run_id[:12]}",
            labels=labels,
            internal=access is PreviewNetworkAccess.ISOLATED,
        )
        created.disconnect = lambda *_args, **_kwargs: None
        return created

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    monkeypatch.setattr(preview_native, "_ensure_preview_image", lambda *_args: None)
    monkeypatch.setattr(preview_native, "_environment_masks", lambda *_args: [])
    monkeypatch.setattr(
        preview_native, "_wait_for_container_health", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(preview_native, "_network", run_network)
    monkeypatch.setattr(
        preview_native,
        "_export_commit",
        lambda _client, _image, source, target, commit: exports.append(
            (source, target, commit)
        ),
    )
    settings = PreviewSettings(
        inspection_image="alpine:latest",
        default_expiry_minutes=30,
        maximum_file_bytes=1_048_576,
        maximum_snapshot_bytes=16_777_216,
        proposal_lifetime_seconds=900,
        build_timeout_seconds=900,
    )
    run_id = "c" * 32
    resources = preview_native._start_native(
        fake_docker_client,
        settings,
        workspace_volume(sandbox_id),
        PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="app:latest",
            start_command="serve",
            container_port=3000,
            network_access=PreviewNetworkAccess.INTERNET,
        ),
        preview_resources._labels(sandbox_id, run_id, None),
        run_id,
        43000,
        controller_store=store,
        kind=PreviewKind.TASK,
        commit_sha="d" * 40,
    )

    application = next(
        call for call in calls if str(call.get("name", "")).endswith("-app")
    )
    runtime_workspace = f"orchestrator-preview-{run_id[:12]}-runtime-workspace"
    assert exports == [(workspace_volume(sandbox_id), runtime_workspace, "d" * 40)]
    assert application["environment"]["DATABASE_URL"] == (
        "file:/var/lib/orchestrator/sqlite/database.sqlite3"
    )
    assert application["volumes"][runtime_workspace] == {
        "bind": "/workspace",
        "mode": "rw",
    }
    assert application["volumes"][db_data_volume(sandbox_id)] == {
        "bind": "/var/lib/orchestrator/sqlite",
        "mode": "rw",
    }
    assert not any("-database" in volume.name for volume in resources["volumes"])

    preview_resources._remove_resources(resources, remove_data_volumes=True)

    assert fake_docker_client.volumes.get(db_data_volume(sandbox_id)).removed is False
    assert fake_docker_client.networks.get(network(sandbox_id)).removed is False


def test_server_preview_borrows_and_disconnects_the_persistent_sandbox_network(
    tmp_path: Path,
    fake_docker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    sandbox_id = "e" * 32
    project_id = "f" * 32
    _managed_database(
        store,
        fake_docker_client,
        sandbox_id=sandbox_id,
        project_id=project_id,
        engine="postgres",
    )
    calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    def run_network(docker_client, run_id, labels, access):
        created = docker_client.networks.create(
            f"orchestrator-preview-{run_id[:12]}",
            labels=labels,
            internal=access is PreviewNetworkAccess.ISOLATED,
        )
        created.disconnect = lambda *_args, **_kwargs: None
        return created

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    monkeypatch.setattr(preview_native, "_ensure_preview_image", lambda *_args: None)
    monkeypatch.setattr(preview_native, "_environment_masks", lambda *_args: [])
    monkeypatch.setattr(preview_native, "_exclude_preview_masks", lambda *_args: None)
    monkeypatch.setattr(
        preview_native, "_wait_for_container_health", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(preview_native, "_network", run_network)
    settings = PreviewSettings(
        inspection_image="alpine:latest",
        default_expiry_minutes=30,
        maximum_file_bytes=1_048_576,
        maximum_snapshot_bytes=16_777_216,
        proposal_lifetime_seconds=900,
        build_timeout_seconds=900,
    )
    run_id = "1" * 32
    resources = preview_native._start_native(
        fake_docker_client,
        settings,
        workspace_volume(sandbox_id),
        PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="app:latest",
            start_command="serve",
            container_port=3000,
            network_access=PreviewNetworkAccess.INTERNET,
        ),
        preview_resources._labels(sandbox_id, run_id, None),
        run_id,
        43001,
        controller_store=store,
    )

    sandbox_network = fake_docker_client.networks.get(network(sandbox_id))
    application = next(
        resource
        for resource in resources["containers"]
        if resource.name.endswith("-app")
    )
    assert application in [
        container for container, _kwargs in sandbox_network.connections
    ]
    assert (
        "DATABASE_URL"
        in next(call for call in calls if str(call.get("name", "")).endswith("-app"))[
            "environment"
        ]
    )

    preview_resources._remove_resources(resources, remove_data_volumes=True)

    assert sandbox_network.connections == []
    assert sandbox_network.removed is False


def test_verification_container_receives_the_sandbox_sqlite_connection(
    tmp_path: Path,
    fake_docker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    sandbox_id = "2" * 32
    project_id = "3" * 32
    _managed_database(
        store, fake_docker_client, sandbox_id=sandbox_id, project_id=project_id
    )
    calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    result = run_verification(
        fake_docker_client,
        VerificationSettings(
            image="verification:latest",
            timeout_seconds=60,
            memory="2g",
            max_output_bytes=1024,
        ),
        volume_name=workspace_volume(sandbox_id),
        commands=[ResolvedVerification(command_kind="test", command="make test")],
        controller_store=store,
        sandbox_id=sandbox_id,
    )

    assert result["passed"] is True
    spec = calls[-1]
    assert spec["network_mode"] == "none"
    assert spec["environment"]["DATABASE_URL"] == (
        "file:/var/lib/orchestrator/sqlite/database.sqlite3"
    )
    assert spec["volumes"][db_data_volume(sandbox_id)] == {
        "bind": "/var/lib/orchestrator/sqlite",
        "mode": "rw",
    }
