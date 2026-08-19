import os
import tempfile
from pathlib import Path
from uuid import uuid4

import docker
import pytest
from fastapi.testclient import TestClient

import app.controller.store as store_module
from app.controller.config import get_controller_settings
from app.controller.store import get_controller_store
from app.docker_client import get_docker_client
from app.main import app
from app.sandboxes import lifecycle as sandbox_lifecycle
from app.sandboxes import router as sandbox_router
from app.sandboxes.manifest import SandboxManifest, write_manifest
from conftest import mark_sandbox_legacy, register_ready_v1_sandbox


PROJECT_ID = "staleness-project"
SANDBOX_ID = "staleness-sandbox"
CURRENT_BASE = "a" * 40
requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


@pytest.fixture
def client(override_docker_client):
    yield TestClient(app)


def _register_v1_staleness_sandbox() -> None:
    store = get_controller_store()
    store.register_v1_project(
        project_id=PROJECT_ID,
        remote_url="https://example.test/staleness/repository.git",
        default_branch="main",
        mirror_volume="staleness-mirror",
        created_at="",
    )
    store.register_v1_sandbox(
        sandbox_id=SANDBOX_ID,
        project_id=PROJECT_ID,
        project_name="staleness repository",
        volume_name="staleness-workspace",
        created_at="",
    )
    write_manifest(
        store,
        SandboxManifest(
            sandbox_id=SANDBOX_ID,
            lifecycle_version="v1",
            feature_key="staleness-check",
            base_ref="refs/heads/main",
            created_base_commit=CURRENT_BASE,
            current_base_commit=CURRENT_BASE,
        ),
    )


def _stub_staleness_commands(monkeypatch: pytest.MonkeyPatch, *, count: int) -> None:
    monkeypatch.setattr(sandbox_router, "fetch_canonical_mirror", lambda *_args, **_kwargs: b"")
    monkeypatch.setattr(
        sandbox_router,
        "count_mirror_staleness",
        lambda *_args, **_kwargs: count,
    )


def test_staleness_fetches_then_returns_count_and_moving_timestamp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_v1_staleness_sandbox()
    store = get_controller_store()
    before = store.project(PROJECT_ID)
    assert before is not None
    assert before["mirror_fetched_at"] is None
    timestamps = iter(
        (
            "2026-08-11T00:00:01Z",
            "2026-08-11T00:00:02Z",
            "2026-08-11T00:00:03Z",
            "2026-08-11T00:00:04Z",
        )
    )
    monkeypatch.setattr(store_module, "_now", lambda: next(timestamps))
    _stub_staleness_commands(monkeypatch, count=3)

    first = client.get(f"/sandboxes/{SANDBOX_ID}/staleness")
    second = client.get(f"/sandboxes/{SANDBOX_ID}/staleness")

    assert first.status_code == 200
    assert first.json() == {
        "behind_count": 3,
        "base_ref": "refs/heads/main",
        "current_base_commit": CURRENT_BASE,
        "mirror_fetched_at": "2026-08-11T00:00:02Z",
        "stale_answer": False,
        "fetch_failure_reason": None,
    }
    assert second.status_code == 200
    assert second.json()["mirror_fetched_at"] == "2026-08-11T00:00:04Z"


def test_staleness_fetch_failure_uses_last_known_mirror_state_without_claiming_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_v1_staleness_sandbox()
    store = get_controller_store()
    with store._connection() as connection:
        connection.execute(
            "UPDATE projects SET mirror_fetched_at = ? WHERE id = ?",
            ("2026-08-11T00:00:00Z", PROJECT_ID),
        )
    monkeypatch.setattr(
        sandbox_router,
        "fetch_canonical_mirror",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("remote unreachable")),
    )
    monkeypatch.setattr(
        sandbox_router,
        "count_mirror_staleness",
        lambda *_args, **_kwargs: 7,
    )

    response = client.get(f"/sandboxes/{SANDBOX_ID}/staleness")

    assert response.status_code == 200
    assert response.json()["behind_count"] == 7
    assert response.json()["mirror_fetched_at"] == "2026-08-11T00:00:00Z"
    assert response.json()["stale_answer"] is True
    assert response.json()["fetch_failure_reason"] == "remote unreachable"
    with store._connection() as connection:
        assert "behind_count" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(projects)")
        }


def test_staleness_does_not_take_a_lifecycle_lease_or_block_an_open_writer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_v1_staleness_sandbox()
    store = get_controller_store()
    store.start_agent_run(
        run_id="agent-1",
        sandbox_id=SANDBOX_ID,
        provider="claude",
        container_id="container-1",
        status="running",
    )
    store.open_agent_writer_session(
        session_id="writer-1",
        sandbox_id=SANDBOX_ID,
        agent_run_id="agent-1",
        kind="terminal",
    )
    _stub_staleness_commands(monkeypatch, count=2)
    # Patched at the source module so the guard holds whichever layer would
    # call it, not just the router.
    monkeypatch.setattr(
        sandbox_lifecycle,
        "lifecycle_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not take lease")),
    )

    response = client.get(f"/sandboxes/{SANDBOX_ID}/staleness")

    assert response.status_code == 200
    assert response.json()["behind_count"] == 2
    assert store.sandbox_lease(SANDBOX_ID) is None


def test_staleness_holds_the_project_mirror_lock_only_during_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_v1_staleness_sandbox()
    store = get_controller_store()

    def fetch(*_args, **_kwargs) -> bytes:
        lock = store.project_mirror_lock(PROJECT_ID)
        assert lock is not None
        assert lock["operation"] == "staleness"
        return b""

    def count(*_args, **_kwargs) -> int:
        assert store.project_mirror_lock(PROJECT_ID) is None
        return 1

    monkeypatch.setattr(sandbox_router, "fetch_canonical_mirror", fetch)
    monkeypatch.setattr(sandbox_router, "count_mirror_staleness", count)

    response = client.get(f"/sandboxes/{SANDBOX_ID}/staleness")

    assert response.status_code == 200
    assert store.project_mirror_lock(PROJECT_ID) is None


@requires_docker
def test_staleness_endpoint_counts_commits_after_a_local_remote_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = docker.from_env()
    run_id = uuid4().hex[:12]
    network = None
    remote_volume = None
    mirror = None
    daemon = None
    previous_override = app.dependency_overrides.get(get_docker_client)
    try:
        network = docker_client.networks.create(name=f"orchestrator-staleness-net-{run_id}")
        remote_volume = docker_client.volumes.create(
            name=f"orchestrator-staleness-remote-{run_id}"
        )
        mirror = docker_client.volumes.create(name=f"orchestrator-staleness-mirror-{run_id}")
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
            # alpine/git ships git without the daemon: `git daemon` exits
            # immediately with "is not a git command", the container dies before
            # Docker publishes a port, and the port lookup below fails with a
            # KeyError that looks nothing like the real cause. Alpine keeps the
            # daemon in its own package.
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
        host_port = int(daemon.attrs["NetworkSettings"]["Ports"]["9418/tcp"][0]["HostPort"])
        remote_url = f"git://host.docker.internal:{host_port}/repository.git"
        # The daemon installs its package before listening, so the port is
        # published a second or two before it accepts connections. Without this
        # wait the fetch fails and staleness correctly degrades to behind_count
        # None - which reads as a product bug rather than a slow fixture.
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
        docker_client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "git init --bare -q /mirror\n"
                "git -C /mirror remote add origin \"$ORCHESTRATOR_REMOTE\"\n"
                "git -C /mirror config --replace-all remote.origin.fetch '+refs/*:refs/*'\n"
                "git -C /mirror symbolic-ref HEAD refs/heads/main\n"
            ],
            remove=True,
            environment={"ORCHESTRATOR_REMOTE": remote_url},
            volumes={mirror.name: {"bind": "/mirror", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        store = get_controller_store()
        store.register_v1_project(
            project_id=PROJECT_ID,
            remote_url="https://example.test/staleness/repository.git",
            default_branch="main",
            mirror_volume=mirror.name,
            created_at="",
        )
        store.register_v1_sandbox(
            sandbox_id=SANDBOX_ID,
            project_id=PROJECT_ID,
            project_name="staleness repository",
            volume_name="staleness-workspace",
            created_at="",
        )
        write_manifest(
            store,
            SandboxManifest(
                sandbox_id=SANDBOX_ID,
                lifecycle_version="v1",
                feature_key="staleness-check",
                base_ref="refs/heads/main",
                created_base_commit=base_commit,
                current_base_commit=base_commit,
            ),
        )
        docker_client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "git clone -q /remote/repository.git /tmp/work\n"
                "git -C /tmp/work config user.name test\n"
                "git -C /tmp/work config user.email test@example.invalid\n"
                "for number in 1 2 3; do\n"
                "  printf '%s' \"$number\" > /tmp/work/\"$number\".txt\n"
                "  git -C /tmp/work add \"$number\".txt\n"
                "  git -C /tmp/work commit -qm \"advance $number\"\n"
                "done\n"
                "git -C /tmp/work push -q origin main\n"
            ],
            remove=True,
            volumes={remote_volume.name: {"bind": "/remote", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        with tempfile.TemporaryDirectory(dir=Path.home()) as secret_directory:
            monkeypatch.setenv("ORCHESTRATOR_GITHUB_READ_TOKEN", "test-read-token")
            monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", secret_directory)
            get_controller_settings.cache_clear()
            app.dependency_overrides[get_docker_client] = lambda: docker_client
            response = TestClient(app).get(f"/sandboxes/{SANDBOX_ID}/staleness")

        assert response.status_code == 200
        assert response.json()["behind_count"] == 3
        assert response.json()["stale_answer"] is False
        assert response.json()["mirror_fetched_at"] is not None
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_docker_client, None)
        else:
            app.dependency_overrides[get_docker_client] = previous_override
        get_controller_settings.cache_clear()
        if daemon is not None:
            daemon.remove(force=True)
        if mirror is not None:
            mirror.remove(force=True)
        if remote_volume is not None:
            remote_volume.remove(force=True)
        if network is not None:
            network.remove()


def test_staleness_refuses_a_migrated_legacy_sandbox(client: TestClient) -> None:
    store = get_controller_store()
    register_ready_v1_sandbox(
        store,
        sandbox_id="legacy-sandbox",
        project_id="legacy-project",
        project_name="legacy",
        volume_name="legacy-workspace",
    )
    mark_sandbox_legacy(store, "legacy-sandbox")

    response = client.get("/sandboxes/legacy-sandbox/staleness")

    assert response.status_code == 409
    # Match the legacy refusal specifically. A later "recreate it" check
    # returns 409 too, so a looser assertion passes without the guard.
    assert response.json()["detail"].startswith("Legacy sandbox")
