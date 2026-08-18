from dataclasses import replace
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker
import pytest
from fastapi.testclient import TestClient

from app.controller.store import get_controller_store
from app.main import app
from app.sandboxes import database as sandbox_database
from app.sandboxes import lifecycle as sandbox_lifecycle
from app.sandboxes import service as sandbox_service
from app.sandboxes.engine_detection import EngineDetection, EngineSignal
from app.sandboxes.manifest import SandboxManifest, read_manifest, write_manifest
from app.sandboxes.naming import mirror_ownership_labels, ownership_labels
from app.sandboxes.git import (
    GitNetworkMode,
    clone_mirror_to_workspace,
    create_workspace_safety_ref,
    ensure_canonical_mirror,
    fetch_canonical_mirror,
    mirror_base_commit,
    require_clean_workspace,
    run_git,
    sync_workspace_from_mirror,
)


PROJECT_ID = "sync-project"
SANDBOX_ID = "sync-sandbox"
OLD_BASE = "a" * 40
NEW_BASE = "b" * 40
requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


@pytest.fixture
def client(override_docker_client):
    yield TestClient(app)


def _register(*, fake_docker_client: Any) -> None:
    store = get_controller_store()
    store.register_v1_project(
        project_id=PROJECT_ID,
        remote_url="https://example.test/sync/repository.git",
        default_branch="main",
        mirror_volume="sync-mirror",
        created_at="",
    )
    store.register_v1_sandbox(
        sandbox_id=SANDBOX_ID,
        project_id=PROJECT_ID,
        project_name="sync repository",
        volume_name="sync-workspace",
        created_at="",
    )
    fake_docker_client.volumes.create(
        name="sync-mirror", labels=mirror_ownership_labels(project_id=PROJECT_ID)
    )
    fake_docker_client.volumes.create(
        name="sync-workspace",
        labels=ownership_labels(sandbox_id=SANDBOX_ID, project_id=PROJECT_ID),
    )
    write_manifest(
        store,
        SandboxManifest(
            sandbox_id=SANDBOX_ID,
            lifecycle_version="v1",
            feature_key="sync-check",
            desired_state="active",
            lifecycle_status="ready",
            feature_branch="feature/sync-check",
            base_ref="refs/heads/main",
            created_base_commit=OLD_BASE,
            current_base_commit=OLD_BASE,
        ),
    )
    store.record_sandbox_engine_detection(
        sandbox_id=SANDBOX_ID,
        signals=[],
        proposed_engine="sqlite",
        migrate_commands=["approved migrate"],
        seed_commands=["approved seed"],
        commands_source={"migrate": "manual", "seed": "manual"},
        detected_at_commit=OLD_BASE,
    )
    store.confirm_sandbox_engine_detection(
        sandbox_id=SANDBOX_ID,
        engine="sqlite",
        migrate_commands=["approved migrate"],
        seed_commands=["approved seed"],
        commands_source={"migrate": "manual", "seed": "manual"},
        actor="tester",
    )


def _stub_git(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(
        sandbox_service,
        "require_clean_workspace",
        lambda *_args, **_kwargs: calls.append("clean"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "create_workspace_safety_ref",
        lambda *_args, **_kwargs: calls.append("safety"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "fetch_canonical_mirror",
        lambda *_args, **_kwargs: calls.append("canonical-fetch"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "mirror_base_commit",
        lambda *_args, **_kwargs: NEW_BASE,
    )
    monkeypatch.setattr(
        sandbox_service,
        "sync_workspace_from_mirror",
        lambda *_args, **_kwargs: calls.append("workspace-fetch-and-rebase"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "discover_engine",
        lambda *_args, **_kwargs: EngineDetection(
            signals=(), proposed_engine="sqlite", migrate_commands=(), seed_commands=(), commands_source={}
        ),
    )


def _complete_sync(*_args: object, **kwargs: object) -> None:
    store = _args[1]
    assert isinstance(store, type(get_controller_store()))
    manifest = read_manifest(store, SANDBOX_ID)
    assert manifest is not None
    detection = store.sandbox_engine_detection(SANDBOX_ID)
    assert detection is not None
    assert 'approved migrate' in str(detection["migrate_commands_json"])
    write_manifest(
        store,
        replace(
            manifest,
            lifecycle_status="ready",
            operation="sync",
            operation_phase="ready",
            current_base_commit=manifest.pending_base_commit,
            pending_base_commit=None,
        ),
    )


def test_sync_happy_path_advances_only_after_approved_migration_snapshot(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    calls: list[str] = []
    _stub_git(monkeypatch, calls)
    monkeypatch.setattr(sandbox_service, "complete_database_provision", _complete_sync)

    response = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})

    assert response.status_code == 202
    assert calls == ["clean", "safety", "canonical-fetch", "workspace-fetch-and-rebase"]
    body = response.json()
    assert body["current_base_commit"] == NEW_BASE
    assert body["pending_base_commit"] is None
    assert body["lifecycle_status"] == "ready"
    assert body["strategy"] == "rebase"
    assert body["safety_ref"] == f"refs/orchestrator/safety/{body['operation_id']}"


def test_sync_merges_only_after_an_observed_open_pull_request(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    manifest = read_manifest(store, SANDBOX_ID)
    assert manifest is not None
    # Intent alone must still use the rebase path.
    write_manifest(store, replace(manifest, pr_requested=True))
    calls: list[str] = []
    _stub_git(monkeypatch, calls)
    monkeypatch.setattr(sandbox_service, "complete_database_provision", _complete_sync)

    first = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})

    assert first.status_code == 202
    assert first.json()["strategy"] == "rebase"
    manifest = read_manifest(store, SANDBOX_ID)
    assert manifest is not None
    write_manifest(store, replace(manifest, current_base_commit=OLD_BASE, lifecycle_status="ready"))
    store.record_sandbox_publication(
        sandbox_id=SANDBOX_ID,
        remote_branch="feature/sync-check",
        last_pushed_commit="c" * 40,
        remote_branch_sha="c" * 40,
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        pr_state="open",
        last_error=None,
    )
    calls.clear()

    second = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})

    assert second.status_code == 202
    assert second.json()["strategy"] == "merge"


def test_sync_preview_requires_opt_in_and_names_the_preview(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    store.create_preview_run(
        {
            "id": "preview-blocker", "sandbox_id": SANDBOX_ID, "proposal_id": "proposal",
            "mode": "native", "kind": "live", "task_id": None, "commit_sha": None,
            "status": "running", "selected_service": None, "container_port": 3000,
            "host_port": None, "config_json": "{}", "config_digest": "digest",
            "network_name": None, "created_at": "", "started_at": None,
            "expires_at": None, "last_activity_at": "",
        }
    )
    refused = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["blocking_writer"] == {"class": "preview", "id": "preview-blocker"}

    def stop(*_args: object, preview_id: str, **_kwargs: object) -> None:
        assert preview_id == "preview-blocker"
        store.update_preview_run(preview_id, status="stopped")

    monkeypatch.setattr(sandbox_lifecycle, "_stop_blocking_preview", stop)
    calls: list[str] = []
    _stub_git(monkeypatch, calls)
    monkeypatch.setattr(sandbox_service, "complete_database_provision", _complete_sync)
    proceeded = client.post(
        f"/sandboxes/{SANDBOX_ID}/sync", json={"stop_blocking_preview": True}
    )
    assert proceeded.status_code == 202


def test_sync_refuses_an_active_delegation(
    client: TestClient, fake_docker_client: Any
) -> None:
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    store.create_planning_session(
        session_id="planning-1",
        project_id=PROJECT_ID,
        sandbox_id=SANDBOX_ID,
        project_name="sync repository",
        title="plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.create_delegation_revision(
        {
            "id": "delegation-1",
            "session_id": "planning-1",
            "sandbox_id": SANDBOX_ID,
            "context_id": None,
            "revision": 1,
            "status": "ready",
        },
        [],
    )

    response = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["blocking_writer"] == {
        "class": "delegation",
        "id": "delegation-1",
    }


def test_sync_allows_idle_agent_but_refuses_open_agent_writer_session(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    store.start_agent_run(run_id="agent-1", sandbox_id=SANDBOX_ID, provider="codex", status="running")
    calls: list[str] = []
    _stub_git(monkeypatch, calls)
    monkeypatch.setattr(sandbox_service, "complete_database_provision", _complete_sync)
    assert client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={}).status_code == 202

    manifest = read_manifest(store, SANDBOX_ID)
    assert manifest is not None
    write_manifest(store, replace(manifest, current_base_commit=OLD_BASE, lifecycle_status="ready"))
    store.open_agent_writer_session(
        session_id="writer-1", sandbox_id=SANDBOX_ID, agent_run_id="agent-1", kind="terminal"
    )
    blocked = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["blocking_writer"] == {
        "class": "agent_writer_session", "id": "writer-1"
    }


def test_git_failure_restores_safety_ref_and_dirty_workspace_changes_nothing(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    calls: list[str] = []
    _stub_git(monkeypatch, calls)
    monkeypatch.setattr(
        sandbox_service, "sync_workspace_from_mirror", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("conflict"))
    )
    monkeypatch.setattr(
        sandbox_service, "restore_workspace_safety_ref", lambda *_args, **_kwargs: calls.append("restored")
    )
    failed = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})
    assert failed.status_code == 409
    assert "restored from safety ref" in failed.json()["detail"]
    manifest = read_manifest(get_controller_store(), SANDBOX_ID)
    assert manifest is not None
    assert manifest.current_base_commit == OLD_BASE
    assert manifest.pending_base_commit is None
    assert manifest.lifecycle_status == "ready"
    assert calls[-1] == "restored"

    before = list(calls)
    monkeypatch.setattr(
        sandbox_service, "require_clean_workspace", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("dirty"))
    )
    dirty = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})
    assert dirty.status_code == 409
    assert calls == before


def test_migration_failure_keeps_pending_and_reset_db_finalizes_recovery(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    calls: list[str] = []
    _stub_git(monkeypatch, calls)

    def fail(_request: object) -> None:
        raise sandbox_database.SandboxDatabaseError(422, "migration exploded")

    with monkeypatch.context() as migration_patch:
        migration_patch.setattr(sandbox_database.SQLITE_DATABASE, "run_migrations", fail)
        failed = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})
    assert failed.status_code == 422
    assert "not rolled back" in failed.json()["detail"]
    manifest = read_manifest(get_controller_store(), SANDBOX_ID)
    assert manifest is not None
    assert manifest.lifecycle_status == "database_failed"
    assert manifest.current_base_commit == OLD_BASE
    assert manifest.pending_base_commit == NEW_BASE

    recovered = client.post(f"/sandboxes/{SANDBOX_ID}/reset-db", json={})
    assert recovered.status_code == 200
    assert recovered.json()["current_base_commit"] == NEW_BASE
    assert recovered.json()["pending_base_commit"] is None
    assert recovered.json()["lifecycle_status"] == "ready"


def test_sync_reports_but_never_applies_an_engine_mismatch(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    calls: list[str] = []
    _stub_git(monkeypatch, calls)
    monkeypatch.setattr(sandbox_service, "complete_database_provision", _complete_sync)
    monkeypatch.setattr(
        sandbox_service,
        "discover_engine",
        lambda *_args, **_kwargs: EngineDetection(
            signals=(EngineSignal("postgres", "test", "x", "changed", 1),),
            proposed_engine="postgres", migrate_commands=(), seed_commands=(), commands_source={}
        ),
    )
    response = client.post(f"/sandboxes/{SANDBOX_ID}/sync", json={})
    assert response.status_code == 202
    assert response.json()["engine_report"] == {
        "confirmed_engine": "sqlite", "detected_engine": "postgres", "mismatch": True,
        "detection_error": None,
    }
    assert get_controller_store().sandbox_engine_detection(SANDBOX_ID)["confirmed_engine"] == "sqlite"


def test_sync_reports_a_database_added_after_no_database_confirmation(
    fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    store.confirm_sandbox_engine_detection(
        sandbox_id=SANDBOX_ID,
        engine="none",
        migrate_commands=[],
        seed_commands=[],
        commands_source={},
        actor="tester",
    )
    monkeypatch.setattr(
        sandbox_service,
        "discover_engine",
        lambda *_args, **_kwargs: EngineDetection(
            signals=(EngineSignal("postgres", "test", "x", "added", 1),),
            proposed_engine="postgres",
            migrate_commands=(),
            seed_commands=(),
            commands_source={},
        ),
    )

    report = sandbox_service.sync_engine_report(
        fake_docker_client,
        store,
        sandbox_id=SANDBOX_ID,
        image="alpine:3.21",
    )

    assert report.confirmed_engine == "none"
    assert report.detected_engine == "postgres"
    assert report.mismatch is True


def test_sync_refuses_legacy_sandbox(client: TestClient) -> None:
    store = get_controller_store()
    store.register_sandbox(
        sandbox_id="legacy-sync", project_id="legacy-project", project_name="legacy",
        source_path="/projects/legacy", volume_name="legacy-workspace", status="ready", created_at="",
    )
    response = client.post("/sandboxes/legacy-sync/sync", json={})
    assert response.status_code == 409
    assert "recreate" in response.json()["detail"]


@requires_docker
def test_sync_git_path_rebases_from_a_local_bare_remote() -> None:
    """Exercise canonical network fetch and local-only sandbox rebase end to end."""
    docker_client = docker.from_env()
    run_id = uuid4().hex[:12]
    network = remote_volume = mirror = workspace = daemon = None

    class StaticCredentialSource:
        def read_token(self) -> str | None:
            return "test-read-token"

    try:
        network = docker_client.networks.create(name=f"orchestrator-sync-net-{run_id}")
        remote_volume = docker_client.volumes.create(name=f"orchestrator-sync-remote-{run_id}")
        mirror = docker_client.volumes.create(name=f"orchestrator-sync-mirror-{run_id}")
        workspace = docker_client.volumes.create(name=f"orchestrator-sync-workspace-{run_id}")
        base_commit = docker_client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "git init --bare -q /remote/repository.git\n"
                "git init -q -b main /tmp/seed\n"
                "git -C /tmp/seed config user.name test\n"
                "git -C /tmp/seed config user.email test@example.invalid\n"
                "printf initial > /tmp/seed/source.txt\n"
                "git -C /tmp/seed add source.txt\n"
                "git -C /tmp/seed commit -qm initial\n"
                "git -C /tmp/seed remote add origin /remote/repository.git\n"
                "git -C /tmp/seed push -q origin main\n"
                "git -C /remote/repository.git symbolic-ref HEAD refs/heads/main\n"
                "git -C /remote/repository.git rev-parse refs/heads/main\n"
            ],
            remove=True,
            volumes={remote_volume.name: {"bind": "/remote", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        ).decode().strip()
        daemon = docker_client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "apk add --no-cache git-daemon >/dev/null 2>&1\n"
                "exec git daemon --reuseaddr --export-all --base-path=/remote /remote\n"
            ],
            detach=True,
            volumes={remote_volume.name: {"bind": "/remote", "mode": "ro"}},
            network=network.name,
            ports={"9418/tcp": ("127.0.0.1", None)},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        daemon.reload()
        port = int(daemon.attrs["NetworkSettings"]["Ports"]["9418/tcp"][0]["HostPort"])
        remote_url = f"git://host.docker.internal:{port}/repository.git"
        # Measured: `apk add git-daemon` needs roughly 30 of these probes
        # before the daemon accepts connections, and a budget of 30 sat exactly
        # on that boundary - the fixture failed intermittently and the symptom
        # looked like a product bug. Keep the budget well clear of it.
        for _ in range(300):
            probe = docker_client.containers.run(
                "alpine/git:latest",
                entrypoint=["sh", "-c"],
                command=[f"git ls-remote {remote_url} >/dev/null 2>&1 && echo ready || echo waiting"],
                remove=True,
                tmpfs={"/git": "rw,nosuid,size=1m"},
            )
            if probe.decode().strip() == "ready":
                break
        else:
            raise AssertionError("git daemon fixture never became reachable")
        with tempfile.TemporaryDirectory(dir=Path.home()) as secret_directory:
            from app.controller.config import get_controller_settings

            previous_secret_directory = os.environ.get("CONTROLLER_GIT_SECRET_DIRECTORY")
            os.environ["CONTROLLER_GIT_SECRET_DIRECTORY"] = secret_directory
            get_controller_settings.cache_clear()
            try:
                default_branch, imported_commit = ensure_canonical_mirror(
                    docker_client, image="alpine/git:latest", mirror_volume=mirror.name,
                    remote_url=remote_url, credential_source=StaticCredentialSource(), ensure_image=True,
                )
                assert (default_branch, imported_commit) == ("main", base_commit)
                clone_mirror_to_workspace(
                    docker_client, image="alpine/git:latest", mirror_volume=mirror.name,
                    workspace_volume=workspace.name, base_commit=base_commit,
                    branch="feature/sync", ensure_image=True,
                )
                docker_client.containers.run(
                    "alpine/git:latest", entrypoint=["sh", "-c"],
                    command=[
                        "set -eu\n"
                        "git clone -q /remote/repository.git /tmp/work\n"
                        "git -C /tmp/work config user.name test\n"
                        "git -C /tmp/work config user.email test@example.invalid\n"
                        "printf advance > /tmp/work/advance.txt\n"
                        "git -C /tmp/work add advance.txt\n"
                        "git -C /tmp/work commit -qm advance\n"
                        "git -C /tmp/work push -q origin main\n"
                    ],
                    remove=True,
                    volumes={remote_volume.name: {"bind": "/remote", "mode": "rw"}},
                    tmpfs={"/git": "rw,nosuid,size=1m"},
                )
                require_clean_workspace(
                    docker_client, image="alpine/git:latest", workspace_volume=workspace.name
                )
                create_workspace_safety_ref(
                    docker_client, image="alpine/git:latest", workspace_volume=workspace.name,
                    safety_ref="refs/orchestrator/safety/test-sync",
                )
                fetch_canonical_mirror(
                    docker_client, image="alpine/git:latest", mirror_volume=mirror.name,
                    credential_source=StaticCredentialSource(),
                )
                new_commit = mirror_base_commit(
                    docker_client, image="alpine/git:latest", mirror_volume=mirror.name,
                    base_ref="refs/heads/main",
                )
                sync_workspace_from_mirror(
                    docker_client, image="alpine/git:latest", mirror_volume=mirror.name,
                    workspace_volume=workspace.name, base_ref="refs/heads/main",
                    pending_base_commit=new_commit, strategy="rebase",
                )
                output = run_git(
                    docker_client, image="alpine/git:latest",
                    volumes={workspace.name: {"bind": "/workspace", "mode": "ro"}},
                    script="git -C /workspace rev-parse HEAD; test -z \"$(git -C /workspace remote)\"",
                    network=GitNetworkMode.NONE,
                )
                assert output.decode().strip() == new_commit
            finally:
                if previous_secret_directory is None:
                    os.environ.pop("CONTROLLER_GIT_SECRET_DIRECTORY", None)
                else:
                    os.environ["CONTROLLER_GIT_SECRET_DIRECTORY"] = previous_secret_directory
                get_controller_settings.cache_clear()
    finally:
        if daemon is not None:
            daemon.remove(force=True)
        if workspace is not None:
            workspace.remove(force=True)
        if mirror is not None:
            mirror.remove(force=True)
        if remote_volume is not None:
            remote_volume.remove(force=True)
        if network is not None:
            network.remove()


def test_publish_requires_review_before_stopping_a_running_preview(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreviewed publish cannot stop a preview before it returns 409."""
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    store.create_preview_run(
        {
            "id": "preview-blocker", "sandbox_id": SANDBOX_ID, "proposal_id": "proposal",
            "mode": "native", "kind": "live", "task_id": None, "commit_sha": None,
            "status": "running", "selected_service": None, "container_port": 3000,
            "host_port": None, "config_json": "{}", "config_digest": "digest",
            "network_name": None, "created_at": "", "started_at": None,
            "expires_at": None, "last_activity_at": "",
        }
    )

    stopped: list[str] = []

    def stop(*_args: object, preview_id: str, **_kwargs: object) -> None:
        stopped.append(preview_id)
        store.update_preview_run(preview_id, status="stopped")

    monkeypatch.setattr(sandbox_lifecycle, "_stop_blocking_preview", stop)

    refused = client.post(
        f"/sandboxes/{SANDBOX_ID}/publish", json={"stop_blocking_preview": True}
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "An approved feature review is required before publish"
    assert stopped == []
    assert store.preview_run("preview-blocker")["status"] == "running"


def test_reviewed_publish_reports_a_blocking_preview_and_takes_the_opt_in(
    client: TestClient, fake_docker_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once reviewed, publish still names a blocking preview and honours the opt-in.

    The review gate now runs first, so it hides this path from an unreviewed
    sandbox. Prove the opt-in that 01f59c1 added still works behind it.
    """
    _register(fake_docker_client=fake_docker_client)
    store = get_controller_store()
    store.create_preview_run(
        {
            "id": "preview-blocker", "sandbox_id": SANDBOX_ID, "proposal_id": "proposal",
            "mode": "native", "kind": "live", "task_id": None, "commit_sha": None,
            "status": "running", "selected_service": None, "container_port": 3000,
            "host_port": None, "config_json": "{}", "config_digest": "digest",
            "network_name": None, "created_at": "", "started_at": None,
            "expires_at": None, "last_activity_at": "",
        }
    )
    monkeypatch.setattr(sandbox_service, "reviewed_target", lambda *_args, **_kwargs: None)

    refused = client.post(f"/sandboxes/{SANDBOX_ID}/publish", json={})

    assert refused.status_code == 409
    assert refused.json()["detail"]["blocking_writer"] == {
        "class": "preview",
        "id": "preview-blocker",
    }
    assert store.preview_run("preview-blocker")["status"] == "running"

    stopped: list[str] = []

    def stop(*_args: object, preview_id: str, **_kwargs: object) -> None:
        stopped.append(preview_id)
        store.update_preview_run(preview_id, status="stopped")

    monkeypatch.setattr(sandbox_lifecycle, "_stop_blocking_preview", stop)

    client.post(f"/sandboxes/{SANDBOX_ID}/publish", json={"stop_blocking_preview": True})

    assert stopped == ["preview-blocker"]
