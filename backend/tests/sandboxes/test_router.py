import inspect
import os
from dataclasses import replace
from uuid import uuid4

import docker
import pytest
from conftest import mark_sandbox_legacy, register_ready_v1_sandbox
from docker.errors import APIError, ContainerError, NotFound
from fastapi.testclient import TestClient

import app.sandboxes.database.mysql as sandbox_database_mysql
import app.sandboxes.database.postgres as sandbox_database_postgres
import app.sandboxes.service.publishing as sandbox_service_publishing
import app.sandboxes.service.resources as sandbox_service_resources
import app.sandboxes.service.transitions as sandbox_service_transitions
from app.agents.models import AgentProvider
from app.controller.store import get_controller_store
from app.controller.store.lifecycle_status import SandboxLifecycleStatus
from app.main import app
from app.planning import service as planning_service
from app.planning.config import PlanningSettings
from app.planning.models import CreatePlanningSessionRequest
from app.platform.docker_client import get_docker_client
from app.platform.naming import (
    database_name,
    db_data_volume,
    mirror_ownership_labels,
    mirror_volume,
    network,
    ownership_labels,
    workspace_volume,
)
from app.projects.models import ProjectRegistration
from app.sandboxes import database as sandbox_database
from app.sandboxes import publish as sandbox_publish
from app.sandboxes import router as sandbox_router
from app.sandboxes import service as sandbox_service
from app.sandboxes.database import sandbox_database_runtime, shared_database_names
from app.sandboxes.engine_detection import NO_DATABASE, EngineDetection, EngineSignal
from app.sandboxes.manifest import (
    read_manifest,
    transition_sandbox_lifecycle,
    write_manifest,
)
from app.sandboxes.mirror import MirrorPin
from app.sandboxes.publish import PublishError, PublishOutcome, PullRequest

REMOTE = "https://github.com/owner/repo.git"
FEATURE_KEY = "add-sandbox-api"
requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


@pytest.fixture
def client(override_docker_client):
    # Deliberately NOT `with TestClient(app)`. The context manager runs the
    # FastAPI lifespan, and the lifespan calls reconcile_controller_state, which
    # builds its own client with docker.from_env() rather than the injected fake.
    # On a machine with real orchestrator volumes that back-fills them into the
    # isolated test database as `discovered` sandboxes, so sandbox counts depend
    # on the developer's Docker state. Every other router test in this suite
    # takes the same bare-client approach for the same reason.
    yield TestClient(app)


@pytest.fixture(autouse=True)
def _stub_canonical_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Router tests use the shared Docker fake, not a real Git remote."""

    def ensure(
        docker_client,
        *,
        image: str,
        project_id: str,
        remote_url: str,
        credential_source=None,
    ) -> MirrorPin:
        name = mirror_volume(project_id)
        try:
            docker_client.volumes.get(name)
        except Exception:  # noqa: BLE001 - fake creates the mirror only when lookup fails
            docker_client.volumes.create(
                name=name,
                driver="local",
                labels=mirror_ownership_labels(project_id=project_id),
            )
        return MirrorPin(volume_name=name, default_branch="main", commit="a" * 40)

    monkeypatch.setattr(sandbox_service_transitions, "ensure_project_mirror", ensure)
    # The Docker fake does not execute Git. Resume identity semantics are
    # exercised here at the router level, while git.py has its own script tests.
    monkeypatch.setattr(
        sandbox_service_transitions, "verify_workspace_identity", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        sandbox_database_mysql,
        "_read_or_create_server_credentials",
        lambda *_args, **_kwargs: {
            "username": "shared-app",
            "password": "shared-app-password",
            "root_password": "root-only-password",
        },
    )
    monkeypatch.setattr(
        sandbox_database_postgres,
        "_read_or_create_server_credentials",
        lambda *_args, **_kwargs: {
            "username": "shared-app",
            "password": "shared-app-password",
            "root_password": "root-only-password",
        },
    )


def _create(client: TestClient, **overrides: object):
    return client.post(
        "/sandboxes",
        json={
            "remote_url": REMOTE,
            "feature_key": FEATURE_KEY,
            **overrides,
        },
    )


def test_create_provisions_deterministic_v1_git_resources(
    client: TestClient, fake_docker_client
) -> None:
    response = _create(client, feature_title="Sandbox API")

    assert response.status_code == 201
    body = response.json()
    assert len(body["sandbox_id"]) == 32
    assert body["lifecycle_version"] == "v1"
    assert body["lifecycle_status"] == "awaiting_engine_confirmation"
    assert body["desired_state"] == "active"
    assert body["feature_branch"] == f"feature/{FEATURE_KEY}"
    assert body["base_ref"] == "refs/heads/main"
    assert body["created_base_commit"] == "a" * 40
    assert body["current_base_commit"] == "a" * 40
    project = get_controller_store().project(body["project_id"])
    assert project is not None
    assert project["mirror_fetched_at"] is not None
    assert len(fake_docker_client.volumes.items) == 2
    assert fake_docker_client.networks.items == []
    # Creation runs a clone helper and an isolated read-only engine detector.
    # It creates no persistent agent, database, or network container.
    assert len(fake_docker_client.containers.items) == 2
    assert all(container.removed for container in fake_docker_client.containers.items)


def test_second_create_resolves_same_sandbox_without_a_sibling(
    client: TestClient,
) -> None:
    first = _create(client)
    second = _create(client)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["sandbox_id"] == first.json()["sandbox_id"]
    store = get_controller_store()
    assert len(store.sandboxes()) == 1


def test_create_waits_for_engine_confirmation_and_releases_its_lease(
    client: TestClient,
) -> None:
    created = _create(client).json()
    store = get_controller_store()

    assert created["lifecycle_status"] == "awaiting_engine_confirmation"
    assert store.sandbox_lease(created["sandbox_id"]) is None

    # Resume is another lifecycle operation. It can claim its own lease while
    # a human considers the engine proposal, and it must not leave the state.
    resumed = client.post(f"/sandboxes/{created['sandbox_id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["lifecycle_status"] == "awaiting_engine_confirmation"


def test_conflicting_engine_signals_wait_and_surface_every_signal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    detection = EngineDetection(
        signals=(
            EngineSignal(
                "mysql", "prisma", "prisma/schema.prisma", "provider = mysql", 1
            ),
            EngineSignal("postgres", "dotenv", ".env", "DATABASE_URL", 2),
        ),
        proposed_engine=None,
        migrate_commands=("npx prisma migrate deploy",),
        seed_commands=(),
        commands_source={"migrate": "prisma"},
    )
    monkeypatch.setattr(
        sandbox_service_transitions,
        "discover_engine",
        lambda *_args, **_kwargs: detection,
    )

    created = _create(client).json()
    record = client.get(f"/sandboxes/{created['sandbox_id']}/engine")

    assert created["lifecycle_status"] == "awaiting_engine_confirmation"
    assert record.status_code == 200
    assert record.json()["proposed_engine"] is None
    assert {
        (signal["engine"], signal["source"]) for signal in record.json()["signals"]
    } == {
        ("mysql", "prisma"),
        ("postgres", "dotenv"),
    }


def test_create_refuses_a_project_that_tracks_its_sqlite_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    detection = EngineDetection(
        signals=(EngineSignal("sqlite", "dotenv", ".env", "DATABASE_URL", 2),),
        proposed_engine="sqlite",
        migrate_commands=(),
        seed_commands=(),
        commands_source={},
        tracked_database_paths=("prisma/dev.db",),
    )
    monkeypatch.setattr(
        sandbox_service_transitions,
        "discover_engine",
        lambda *_args, **_kwargs: detection,
    )

    response = _create(client)

    assert response.status_code == 409
    assert "prisma/dev.db" in response.json()["detail"]


def test_confirm_engine_freezes_snapshot_claims_a_fresh_lease_and_advances(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_docker_client
) -> None:
    created = _create(client).json()
    store = get_controller_store()
    claimed: list[str] = []
    acquire = store.acquire_sandbox_lease

    def capture(**kwargs: object):
        claimed.append(str(kwargs["operation"]))
        return acquire(**kwargs)

    monkeypatch.setattr(store, "acquire_sandbox_lease", capture)
    response = client.post(
        f"/sandboxes/{created['sandbox_id']}/confirm-engine",
        json={
            "engine": "postgres",
            "migrate_commands": ["make migrate"],
            "seed_commands": ["make seed"],
            "commands_source": {"migrate": "makefile", "seed": "makefile"},
            "actor": "jerome",
        },
    )

    assert response.status_code == 200
    assert claimed == ["confirm-engine"]
    body = response.json()
    assert body["lifecycle_status"] == "ready"
    assert body["db_engine"] == "postgres"
    assert body["db_name"] == database_name(created["sandbox_id"])
    database = get_controller_store().sandbox_database(created["sandbox_id"])
    assert database is not None
    assert database["status"] == "ready"
    assert database["engine"] == "postgres"
    assert database["db_name"] == body["db_name"]
    project_id = body["project_id"]
    assert fake_docker_client.containers.get(
        shared_database_names(project_id, "postgres")["container"]
    )
    assert fake_docker_client.networks.get(network(created["sandbox_id"]))
    assert get_controller_store().sandbox_lease(created["sandbox_id"]) is None
    record = client.get(f"/sandboxes/{created['sandbox_id']}/engine").json()
    assert record["confirmed_engine"] == "postgres"
    assert record["migrate_commands"] == ["make migrate"]
    assert record["commands_source"] == {"migrate": "makefile", "seed": "makefile"}
    assert record["actor"] == "jerome"
    assert record["confirmed_at"]


def test_confirm_engine_requires_commands_when_detection_has_none(
    client: TestClient,
) -> None:
    created = _create(client).json()

    response = client.post(
        f"/sandboxes/{created['sandbox_id']}/confirm-engine",
        json={"engine": "mysql", "actor": "jerome"},
    )

    assert response.status_code == 422
    assert client.get(f"/sandboxes/{created['sandbox_id']}").json()[
        "lifecycle_status"
    ] == ("awaiting_engine_confirmation")


def test_confirm_no_database_reaches_ready_without_database_resources(
    client: TestClient, fake_docker_client
) -> None:
    created = _create(client).json()

    response = client.post(
        f"/sandboxes/{created['sandbox_id']}/confirm-engine",
        json={"engine": "none", "actor": "jerome"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle_status"] == "ready"
    assert body["db_engine"] == NO_DATABASE
    assert body["db_name"] is None
    assert body["db_data_volume"] is None
    assert get_controller_store().sandbox_database(created["sandbox_id"]) is None
    assert (
        sandbox_database_runtime(
            fake_docker_client, get_controller_store(), created["sandbox_id"]
        )
        is None
    )
    assert fake_docker_client.networks.items == []


def test_reset_database_refuses_a_no_database_sandbox(client: TestClient) -> None:
    created = _create(client).json()
    confirmed = client.post(
        f"/sandboxes/{created['sandbox_id']}/confirm-engine",
        json={"engine": "none", "actor": "jerome"},
    )
    assert confirmed.status_code == 200

    response = client.post(f"/sandboxes/{created['sandbox_id']}/reset-db", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Sandbox '{created['sandbox_id']}' has no database to reset"
    )


@requires_docker
def test_no_database_provision_and_destroy_leave_no_database_residue() -> None:
    docker_client = docker.from_env()
    sandbox_id = f"no-database-{uuid4().hex}"
    project_id = f"no-database-project-{uuid4().hex}"
    workspace = workspace_volume(sandbox_id)
    store = get_controller_store()
    previous_override = app.dependency_overrides.get(get_docker_client)
    try:
        store.register_v1_project(
            project_id=project_id,
            remote_url="https://example.test/no-database/repository.git",
            default_branch="main",
            mirror_volume=f"no-database-mirror-{uuid4().hex}",
            created_at="",
        )
        store.register_v1_sandbox(
            sandbox_id=sandbox_id,
            project_id=project_id,
            project_name="no database repository",
            volume_name=workspace,
            created_at="",
        )
        docker_client.volumes.create(
            name=workspace,
            labels=ownership_labels(sandbox_id=sandbox_id, project_id=project_id),
        )
        store.record_sandbox_resource(sandbox_id, kind="volume", name=workspace)
        write_manifest(
            store,
            sandbox_router.SandboxManifest(
                sandbox_id=sandbox_id,
                lifecycle_version="v1",
                feature_key="no-database",
                desired_state="active",
                lifecycle_status="creating",
                db_engine="none",
            ),
        )
        store.record_sandbox_engine_detection(
            sandbox_id=sandbox_id,
            signals=[],
            proposed_engine="none",
            migrate_commands=[],
            seed_commands=[],
            commands_source={},
            detected_at_commit="a" * 40,
        )
        store.confirm_sandbox_engine_detection(
            sandbox_id=sandbox_id,
            engine="none",
            migrate_commands=[],
            seed_commands=[],
            commands_source={},
            actor="tester",
        )

        sandbox_service.complete_database_provision(
            docker_client,
            store,
            sandbox_id=sandbox_id,
            operation="confirm-engine",
            rebuild=False,
        )

        manifest = read_manifest(store, sandbox_id)
        assert manifest is not None
        assert manifest.lifecycle_status == "ready"
        assert manifest.db_name is None
        assert manifest.db_data_volume is None
        assert store.sandbox_database(sandbox_id) is None
        assert docker_client.networks.list(names=[network(sandbox_id)]) == []
        assert (
            docker_client.volumes.list(filters={"name": db_data_volume(sandbox_id)})
            == []
        )
        assert (
            docker_client.containers.list(
                all=True,
                filters={"label": f"orchestrator.sandbox.id={sandbox_id}"},
            )
            == []
        )

        def override():
            yield docker_client

        app.dependency_overrides[get_docker_client] = override
        deleted = TestClient(app).delete(f"/sandboxes/{sandbox_id}")

        assert deleted.status_code == 200
        with pytest.raises(NotFound):
            docker_client.volumes.get(workspace)
        assert docker_client.networks.list(names=[network(sandbox_id)]) == []
        assert (
            docker_client.volumes.list(filters={"name": db_data_volume(sandbox_id)})
            == []
        )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_docker_client, None)
        else:
            app.dependency_overrides[get_docker_client] = previous_override
        try:
            docker_client.volumes.get(workspace).remove(force=True)
        except NotFound:
            pass


def test_human_confirmation_at_create_skips_the_waiting_state(
    client: TestClient, fake_docker_client
) -> None:
    response = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["lifecycle_status"] == "ready"
    assert body["db_engine"] == "sqlite"
    assert body["db_name"] == database_name(body["sandbox_id"])
    assert body["db_data_volume"] == db_data_volume(body["sandbox_id"])
    database = get_controller_store().sandbox_database(body["sandbox_id"])
    assert database is not None
    assert database["status"] == "ready"
    assert database["engine"] == "sqlite"
    assert database["db_name"] == body["db_name"]
    assert fake_docker_client.volumes.get(body["db_data_volume"])
    assert fake_docker_client.networks.get(network(body["sandbox_id"]))
    record = client.get(f"/sandboxes/{body['sandbox_id']}/engine").json()
    assert record["confirmed_engine"] == "sqlite"


def test_second_create_does_not_reprovision_the_sandbox_database(
    client: TestClient, fake_docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    confirmation = {
        "engine": "sqlite",
        "migrate_commands": ["make migrate"],
        "commands_source": {"migrate": "makefile"},
        "actor": "jerome",
    }
    first = _create(client, engine_confirmation=confirmation)
    before = get_controller_store().sandbox_database(first.json()["sandbox_id"])
    second = _create(client, engine_confirmation=confirmation)
    after = get_controller_store().sandbox_database(first.json()["sandbox_id"])

    assert first.status_code == 201
    assert second.status_code == 200
    assert before is not None and after is not None
    assert after["provisioned_at"] == before["provisioned_at"]
    assert (
        len(
            [
                call
                for call in calls
                if str(call.get("name", "")).endswith("-database-migrate")
            ]
        )
        == 1
    )


def test_migration_runner_has_only_sandbox_database_authority(
    client: TestClient, fake_docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    response = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "seed_commands": ["make seed"],
            "commands_source": {"migrate": "makefile", "seed": "makefile"},
            "actor": "jerome",
        },
    )

    sandbox_id = response.json()["sandbox_id"]
    runner = next(
        call
        for call in calls
        if str(call.get("name", "")).endswith("-database-migrate")
    )
    assert runner["command"] == [
        "sh",
        "-lc",
        "set -eu\nmake migrate\nmake seed",
    ]
    assert runner["read_only"] is True
    assert runner["cap_drop"] == ["ALL"]
    assert runner["security_opt"] == ["no-new-privileges:true"]
    assert runner["pids_limit"] == 256
    assert runner["mem_limit"] == "4g"
    assert runner["network_mode"] == "none"
    assert "network" not in runner
    assert runner["environment"] == {
        "DATABASE_URL": "file:/var/lib/orchestrator/sqlite/database.sqlite3"
    }
    assert runner["volumes"] == {
        workspace_volume(sandbox_id): {"bind": "/workspace", "mode": "rw"},
        db_data_volume(sandbox_id): {
            "bind": "/var/lib/orchestrator/sqlite",
            "mode": "rw",
        },
    }
    serialized = repr(runner).casefold()
    assert "github" not in serialized
    assert "controller_data" not in serialized
    assert "root_password" not in serialized
    assert "docker.sock" not in serialized
    assert "subprocess" not in inspect.getsource(sandbox_database)


def test_reset_database_converges_and_finalizes_a_pending_base(
    client: TestClient,
    fake_docker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    store = get_controller_store()
    manifest = read_manifest(store, created["sandbox_id"])
    assert manifest is not None
    pending = "b" * 40
    assert transition_sandbox_lifecycle(
        store,
        replace(
            manifest,
            pending_base_commit=pending,
        ),
        to_status=SandboxLifecycleStatus.CREATING,
    )
    manifest = read_manifest(store, created["sandbox_id"])
    assert manifest is not None
    assert transition_sandbox_lifecycle(
        store,
        manifest,
        to_status=SandboxLifecycleStatus.DATABASE_FAILED,
    )
    create_calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        create_calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)

    first = client.post(f"/sandboxes/{created['sandbox_id']}/reset-db", json={})
    second = client.post(f"/sandboxes/{created['sandbox_id']}/reset-db", json={})

    assert first.status_code == second.status_code == 200
    assert first.json()["lifecycle_status"] == "ready"
    assert first.json()["current_base_commit"] == pending
    assert first.json()["pending_base_commit"] is None
    assert second.json()["current_base_commit"] == pending
    assert store.sandbox_database(created["sandbox_id"])["status"] == "ready"
    assert (
        len(
            [
                call
                for call in create_calls
                if "rm -f /database/database.sqlite3" in str(call["command"])
            ]
        )
        == 2
    )


def test_reset_database_refuses_a_preview_then_honors_explicit_stop(
    client: TestClient,
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    store = get_controller_store()
    preview_id = "preview-blocker"
    store.create_preview_run(
        {
            "id": preview_id,
            "sandbox_id": created["sandbox_id"],
            "proposal_id": "proposal",
            "mode": "native",
            "kind": "live",
            "task_id": None,
            "commit_sha": None,
            "status": "preparing",
            "selected_service": None,
            "container_port": 3000,
            "host_port": None,
            "config_json": "{}",
            "config_digest": "digest",
            "network_name": None,
            "created_at": "2026-08-11T00:00:00Z",
            "started_at": None,
            "expires_at": None,
            "last_activity_at": "2026-08-11T00:00:00Z",
        }
    )
    refused = client.post(f"/sandboxes/{created['sandbox_id']}/reset-db", json={})

    assert refused.status_code == 409
    assert refused.json()["detail"]["blocking_writer"] == {
        "class": "preview",
        "id": preview_id,
    }

    proceeded = client.post(
        f"/sandboxes/{created['sandbox_id']}/reset-db",
        json={"stop_blocking_preview": True},
    )
    assert proceeded.status_code == 200
    assert proceeded.json()["lifecycle_status"] == "ready"


def test_migration_failure_never_marks_the_sandbox_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_request: object):
        raise sandbox_database.SandboxDatabaseError(422, "migration exploded")

    monkeypatch.setattr(sandbox_database.SQLITE_DATABASE, "run_migrations", fail)
    response = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    )

    assert response.status_code == 422
    sandbox = get_controller_store().sandboxes()[0]
    assert sandbox["lifecycle_status"] == "database_failed"
    assert sandbox["lifecycle_status"] != "ready"


def test_destroy_sweeps_only_manifest_resources_and_keeps_shared_infrastructure(
    client: TestClient, fake_docker_client
) -> None:
    first = _create(client).json()
    second = _create(client, feature_key="second-sandbox").json()
    project_id = first["project_id"]
    dependency = fake_docker_client.volumes.create(
        name="dependency-lockfile-volume",
        labels={"orchestrator.project.id": project_id},
    )
    shared_database = fake_docker_client.containers.create(
        name="shared-database",
        image="db",
        labels={"orchestrator.project.id": project_id},
    )

    removed = client.delete(f"/sandboxes/{first['sandbox_id']}")

    assert removed.status_code == 200
    assert fake_docker_client.volumes.get(mirror_volume(project_id)).removed is False
    assert dependency.removed is False
    assert shared_database.removed is False
    assert (
        fake_docker_client.volumes.get(workspace_volume(second["sandbox_id"])).removed
        is False
    )
    assert first["sandbox_id"] not in {
        row["id"] for row in get_controller_store().sandboxes()
    }


def test_destroy_removes_sqlite_database_volume_and_sandbox_network(
    client: TestClient, fake_docker_client
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    database_volume = db_data_volume(created["sandbox_id"])
    database_network = network(created["sandbox_id"])

    response = client.delete(f"/sandboxes/{created['sandbox_id']}")

    assert response.status_code == 200
    with pytest.raises(NotFound):
        fake_docker_client.volumes.get(database_volume)
    with pytest.raises(NotFound):
        fake_docker_client.networks.get(database_network)
    assert fake_docker_client.volumes.get(mirror_volume(created["project_id"]))


def test_destroy_drops_postgres_database_and_preserves_shared_server(
    client: TestClient,
    fake_docker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        create_calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    waiting = _create(client).json()
    confirmed = client.post(
        f"/sandboxes/{waiting['sandbox_id']}/confirm-engine",
        json={
            "engine": "postgres",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    names = shared_database_names(confirmed["project_id"], "postgres")
    server = fake_docker_client.containers.get(names["container"])

    response = client.delete(f"/sandboxes/{confirmed['sandbox_id']}")

    assert response.status_code == 200
    drop_sql = [
        str(call.get("environment", {}).get("PREVIEW_SQL", ""))
        for call in create_calls
        if "PREVIEW_SQL" in call.get("environment", {})
    ][-1]
    assert f'DROP DATABASE IF EXISTS "{confirmed["db_name"]}"' in drop_sql
    assert f'DROP ROLE IF EXISTS "{confirmed["db_name"]}"' in drop_sql
    assert server.removed is False
    assert fake_docker_client.networks.get(names["network"]).removed is False
    assert fake_docker_client.volumes.get(names["data"]).removed is False
    with pytest.raises(NotFound):
        fake_docker_client.networks.get(network(confirmed["sandbox_id"]))


def test_repeated_destroy_returns_the_tombstone(client: TestClient) -> None:
    sandbox = _create(client).json()

    first = client.delete(f"/sandboxes/{sandbox['sandbox_id']}")
    second = client.delete(f"/sandboxes/{sandbox['sandbox_id']}")

    assert first.status_code == second.status_code == 200
    assert second.json()["sandbox_id"] == sandbox["sandbox_id"]


def test_destroy_refuses_a_manifest_resource_with_wrong_labels(
    client: TestClient, fake_docker_client
) -> None:
    sandbox = _create(client).json()
    workspace = fake_docker_client.volumes.get(workspace_volume(sandbox["sandbox_id"]))
    workspace.labels.clear()
    workspace.attrs["Labels"] = workspace.labels

    response = client.delete(f"/sandboxes/{sandbox['sandbox_id']}")

    assert response.status_code == 500
    row = get_controller_store().sandbox(sandbox["sandbox_id"])
    assert row is not None
    assert row["lifecycle_status"] == "destroying"
    assert get_controller_store().sandbox_tombstone(sandbox["sandbox_id"]) is None


def test_resume_recreates_only_a_missing_workspace_and_preserves_present_worktree(
    client: TestClient, fake_docker_client
) -> None:
    sandbox = _create(client).json()
    workspace_name = workspace_volume(sandbox["sandbox_id"])
    before = fake_docker_client.volumes.get(workspace_name)

    present = client.post(f"/sandboxes/{sandbox['sandbox_id']}/resume")
    assert present.status_code == 200
    assert fake_docker_client.volumes.get(workspace_name) is before

    before.remove(force=True)
    recreated = client.post(f"/sandboxes/{sandbox['sandbox_id']}/resume")
    assert recreated.status_code == 200
    assert fake_docker_client.volumes.get(workspace_name) is not before


def test_resume_recovers_a_missing_workspace_but_reraises_other_workspace_errors(
    client: TestClient, fake_docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    imported_workspaces: list[str] = []
    ensure_workspace_import = sandbox_service.ensure_workspace_import

    def record_workspace_import(*args, **kwargs) -> None:
        imported_workspaces.append(kwargs["sandbox_id"])
        ensure_workspace_import(*args, **kwargs)

    monkeypatch.setattr(
        sandbox_service_transitions, "ensure_workspace_import", record_workspace_import
    )
    recoverable = _create(client).json()
    imported_workspaces.clear()
    recoverable_workspace = fake_docker_client.volumes.get(
        workspace_volume(recoverable["sandbox_id"])
    )
    recoverable_workspace.remove(force=True)

    recovered = client.post(f"/sandboxes/{recoverable['sandbox_id']}/resume")

    assert recovered.status_code == 200
    assert imported_workspaces == [recoverable["sandbox_id"]]
    assert fake_docker_client.volumes.get(
        workspace_volume(recoverable["sandbox_id"])
    ) is not (recoverable_workspace)

    rejected = _create(client, feature_key="reject-workspace-error").json()
    imported_workspaces.clear()
    rejected_workspace = fake_docker_client.volumes.get(
        workspace_volume(rejected["sandbox_id"])
    )
    monkeypatch.setattr(
        sandbox_service_transitions,
        "verify_workspace_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("identity check failed")
        ),
    )

    response = client.post(f"/sandboxes/{rejected['sandbox_id']}/resume")

    assert response.status_code == 409
    assert imported_workspaces == []
    assert (
        fake_docker_client.volumes.get(workspace_volume(rejected["sandbox_id"]))
        is rejected_workspace
    )
    manifest = read_manifest(get_controller_store(), rejected["sandbox_id"])
    assert manifest is not None
    assert manifest.lifecycle_status is SandboxLifecycleStatus.DEGRADED


def test_resume_recovers_a_workspace_phase_failure_without_engine_detection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_workspace_import = sandbox_service.ensure_workspace_import
    monkeypatch.setattr(
        sandbox_service_transitions,
        "ensure_workspace_import",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("workspace import failed")
        ),
    )

    failed = _create(client)

    assert failed.status_code == 500
    store = get_controller_store()
    sandbox = store.sandboxes()[0]
    sandbox_id = str(sandbox["id"])
    manifest = read_manifest(store, sandbox_id)
    assert manifest is not None
    assert manifest.lifecycle_status == "creating"
    assert store.sandbox_engine_detection(sandbox_id) is None

    monkeypatch.setattr(
        sandbox_service_transitions, "ensure_workspace_import", ensure_workspace_import
    )
    resumed = client.post(f"/sandboxes/{sandbox_id}/resume")

    assert resumed.status_code == 200
    assert resumed.json()["lifecycle_status"] == "awaiting_engine_confirmation"
    assert store.sandbox_engine_detection(sandbox_id) is not None

    confirmed = client.post(
        f"/sandboxes/{sandbox_id}/confirm-engine",
        json={"engine": "none", "actor": "tester"},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["lifecycle_status"] == "ready"


def test_resume_reuses_an_unconfirmed_engine_detection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create(client).json()
    store = get_controller_store()
    manifest = read_manifest(store, sandbox["sandbox_id"])
    assert manifest is not None
    transition_sandbox_lifecycle(
        store,
        manifest,
        to_status=SandboxLifecycleStatus.CREATING,
    )
    monkeypatch.setattr(
        sandbox_service_transitions,
        "discover_engine",
        lambda *_args, **_kwargs: pytest.fail("resume must reuse the stored detection"),
    )

    resumed = client.post(f"/sandboxes/{sandbox['sandbox_id']}/resume")

    assert resumed.status_code == 200
    assert resumed.json()["lifecycle_status"] == "awaiting_engine_confirmation"


def test_resume_marks_unsafe_ownership_inconsistency_degraded(
    client: TestClient, fake_docker_client
) -> None:
    sandbox = _create(client).json()
    workspace = fake_docker_client.volumes.get(workspace_volume(sandbox["sandbox_id"]))
    workspace.labels.clear()

    response = client.post(f"/sandboxes/{sandbox['sandbox_id']}/resume")

    assert response.status_code == 409
    row = get_controller_store().sandbox(sandbox["sandbox_id"])
    assert row is not None
    assert row["lifecycle_status"] == "degraded"


def test_destroy_failure_keeps_destroying_without_tombstone_and_retry_completes(
    client: TestClient, fake_docker_client
) -> None:
    sandbox = _create(client).json()
    fake_docker_client.inject_failure("volume.remove", APIError("busy"))

    failed = client.delete(f"/sandboxes/{sandbox['sandbox_id']}")
    assert failed.status_code == 500
    assert get_controller_store().sandbox_tombstone(sandbox["sandbox_id"]) is None
    row = get_controller_store().sandbox(sandbox["sandbox_id"])
    assert row is not None and row["lifecycle_status"] == "destroying"
    assert row["last_error"]

    retried = client.delete(f"/sandboxes/{sandbox['sandbox_id']}")
    assert retried.status_code == 200
    assert get_controller_store().sandbox_tombstone(sandbox["sandbox_id"]) is not None


def test_orphan_removal_reports_a_docker_outage_as_unavailable(
    client: TestClient,
    fake_docker_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon failure during removal is 503, and keeps Docker's own message."""
    sandbox = _create(client).json()
    name = f"sbx-{sandbox['sandbox_id'][:12]}-outage"
    fake_docker_client.volumes.create(
        name=name,
        labels=ownership_labels(
            sandbox_id=sandbox["sandbox_id"], project_id=sandbox["project_id"]
        ),
    )
    store = get_controller_store()
    store.event(
        sandbox_id=None,
        run_id=None,
        kind="controller.unexpected_resource",
        payload={
            "resource": f"volume:{name}",
            "resource_kind": "volume",
            "resource_name": name,
        },
    )
    monkeypatch.setattr(
        sandbox_service_resources,
        "_remove_manifest_resource",
        lambda *_a, **_k: (_ for _ in ()).throw(APIError("daemon said no")),
    )

    response = client.post(f"/sandboxes/orphans/volume:{name}/remove")

    assert response.status_code == 503
    assert "daemon said no" in response.json()["detail"]
    assert fake_docker_client.volumes.get(name).removed is False


def test_orphan_removal_revalidates_manifest_ownership(
    client: TestClient,
    fake_docker_client,
) -> None:
    sandbox = _create(client).json()
    name = f"sbx-{sandbox['sandbox_id'][:12]}-late-claim"
    fake_docker_client.volumes.create(
        name=name,
        labels=ownership_labels(
            sandbox_id=sandbox["sandbox_id"], project_id=sandbox["project_id"]
        ),
    )
    store = get_controller_store()
    store.event(
        sandbox_id=None,
        run_id=None,
        kind="controller.unexpected_resource",
        payload={
            "resource": f"volume:{name}",
            "resource_kind": "volume",
            "resource_name": name,
        },
    )
    store.record_sandbox_resource(sandbox["sandbox_id"], kind="volume", name=name)

    response = client.post(f"/sandboxes/orphans/volume:{name}/remove")

    assert response.status_code == 409
    assert fake_docker_client.volumes.get(name).removed is False


def test_orphans_route_reads_the_startup_report_without_a_docker_mutation(
    client: TestClient,
) -> None:
    store = get_controller_store()
    store.event(
        sandbox_id=None,
        run_id=None,
        kind="controller.unexpected_resource",
        payload={
            "resource": "volume:sbx-reported-volume",
            "resource_kind": "volume",
            "resource_name": "sbx-reported-volume",
        },
    )

    response = client.get("/sandboxes/orphans")

    assert response.status_code == 200
    assert response.json()["resources"][0]["resource"] == "volume:sbx-reported-volume"


def test_orphans_route_hides_a_reported_resource_now_claimed_by_a_manifest(
    client: TestClient,
) -> None:
    sandbox = _create(client).json()
    name = f"sbx-{sandbox['sandbox_id'][:12]}-late-claim"
    store = get_controller_store()
    store.event(
        sandbox_id=None,
        run_id=None,
        kind="controller.unexpected_resource",
        payload={
            "resource": f"volume:{name}",
            "resource_kind": "volume",
            "resource_name": name,
        },
    )
    store.record_sandbox_resource(sandbox["sandbox_id"], kind="volume", name=name)

    response = client.get("/sandboxes/orphans")

    assert response.status_code == 200
    assert response.json() == {"count": 0, "resources": []}


@pytest.mark.parametrize(
    ("kind", "name", "labels"),
    [
        ("volume", "sbx-mirror-looking", {"orchestrator.project.mirror": "true"}),
        (
            "container",
            "sbx-shared-database-looking",
            {"orchestrator.shared-database": "true"},
        ),
        (
            "volume",
            "sbx-shared-database-data-looking",
            {"orchestrator.shared-database": "true"},
        ),
        (
            "network",
            "sbx-shared-database-network-looking",
            {"orchestrator.shared-database": "true"},
        ),
        (
            "volume",
            "sbx-dependency-looking",
            {
                "orchestrator.preview.data-managed": "true",
                "orchestrator.preview.persistent": "true",
            },
        ),
    ],
)
def test_orphan_removal_refuses_shared_infrastructure(
    client: TestClient,
    fake_docker_client,
    kind: str,
    name: str,
    labels: dict[str, str],
) -> None:
    collection = getattr(fake_docker_client, f"{kind}s")
    if kind == "container":
        docker_resource = collection.create(name=name, image="test", labels=labels)
    elif kind == "network":
        docker_resource = collection.create(name, labels=labels)
    else:
        docker_resource = collection.create(name=name, labels=labels)

    response = client.post(f"/sandboxes/orphans/{kind}:{name}/remove")

    assert response.status_code == 409
    assert docker_resource.removed is False


def test_equivalent_remote_forms_resolve_one_project_and_one_sandbox(
    client: TestClient,
) -> None:
    remotes = [
        "git@github.com:owner/repo.git",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
    ]

    responses = [_create(client, remote_url=remote) for remote in remotes]

    assert [response.status_code for response in responses] == [201, 200, 200]
    assert len({response.json()["project_id"] for response in responses}) == 1
    assert len({response.json()["sandbox_id"] for response in responses}) == 1
    store = get_controller_store()
    assert len(store.sandboxes()) == 1
    assert len({row["project_id"] for row in store.sandboxes()}) == 1


def test_token_remote_is_stripped_before_response_and_persistence(
    client: TestClient,
) -> None:
    token = "secret-token-value"
    response = _create(
        client,
        remote_url=f"https://{token}@GitHub.com/owner/repo.git",
    )

    assert response.status_code == 201
    assert token not in response.text
    assert response.json()["remote_url"] == "https://github.com/owner/repo"
    sandbox = get_controller_store().sandboxes()[0]
    project = get_controller_store().project(str(sandbox["project_id"]))
    assert project is not None
    assert token not in str(project)


@pytest.mark.parametrize(
    "payload",
    [
        {"remote_url": REMOTE},
        {"remote_url": REMOTE, "feature_key": "bad_key"},
        {"remote_url": REMOTE, "feature_key": "a"},
        {"remote_url": REMOTE, "feature_title": "No fallback"},
    ],
)
def test_create_requires_a_valid_human_feature_key(
    client: TestClient, payload: dict[str, str]
) -> None:
    response = client.post("/sandboxes", json=payload)

    assert response.status_code == 422
    assert get_controller_store().sandboxes() == []


def test_create_refuses_a_project_without_a_remote(client: TestClient) -> None:
    response = client.post("/sandboxes", json={"feature_key": FEATURE_KEY})

    assert response.status_code == 400
    assert response.json()["detail"] == "Sandbox creation requires a Git remote."


def test_list_get_and_unknown_sandbox(client: TestClient) -> None:
    created = _create(client).json()

    listed = client.get("/sandboxes")
    fetched = client.get(f"/sandboxes/{created['sandbox_id']}")
    unknown = client.get("/sandboxes/not-a-sandbox")

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["sandboxes"] == [created]
    assert fetched.status_code == 200
    assert fetched.json() == created
    assert unknown.status_code == 404


def test_planning_attaches_to_an_existing_sandbox_without_creating_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create(client).json()
    store = get_controller_store()
    sandbox = store.sandbox(created["sandbox_id"])
    assert sandbox is not None
    project = ProjectRegistration(
        sandbox_id=created["sandbox_id"],
        name="Existing v1 sandbox",
        source_path=f"managed:{created['project_id']}",
        volume_name=str(sandbox["volume_name"]),
        created_at=str(sandbox["created_at"]),
        ready=True,
    )
    monkeypatch.setattr(planning_service, "schedule_turn", lambda *_: None)
    monkeypatch.setattr(
        planning_service,
        "ensure_sandbox_registered",
        lambda *_: (created["sandbox_id"], created["project_id"], project),
    )
    settings = PlanningSettings(
        clarifier_provider=AgentProvider.CLAUDE,
        planner_provider=AgentProvider.CLAUDE,
        reviewer_provider=AgentProvider.CODEX,
        credential_profile="default",
        max_review_turns=3,
        turn_timeout_seconds=10,
        planning_memory="2g",
        claude_model="opus",
        codex_model="gpt-5.6-terra",
        codex_reasoning_effort="high",
    )

    session = planning_service.create_session(
        object(),
        store,
        settings,
        project.name,
        CreatePlanningSessionRequest(title="Plan it", request="Plan the feature"),
    )

    assert session.sandbox_id == created["sandbox_id"]
    assert len(store.sandboxes()) == 1


def test_publish_records_verified_git_and_pull_request_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    calls: list[dict[str, object]] = []

    def publish(*_args: object, **kwargs: object) -> PublishOutcome:
        calls.append(dict(kwargs))
        return PublishOutcome(
            remote_branch="feature/add-sandbox-api",
            last_pushed_commit="b" * 40,
            remote_branch_sha="b" * 40,
            pushed=True,
        )

    monkeypatch.setattr(
        sandbox_service_publishing, "reviewed_target", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(sandbox_service_publishing, "publish_reviewed_feature", publish)
    monkeypatch.setattr(
        sandbox_service_publishing,
        "discover_or_create_pull_request",
        lambda **_kwargs: PullRequest(
            number=42,
            url="https://github.com/owner/repo/pull/42",
            state="closed",
            merged_at="2026-08-14T00:00:00Z",
        ),
    )

    response = client.post(f"/sandboxes/{created['sandbox_id']}/publish")

    assert response.status_code == 202
    assert calls and calls[0]["feature_branch"] == "feature/add-sandbox-api"
    body = response.json()
    assert body["last_pushed_commit"] == "b" * 40
    assert body["remote_branch_sha"] == "b" * 40
    assert body["pr_number"] == 42
    assert body["pr_merged_at"] == "2026-08-14T00:00:00Z"
    publication = client.get(f"/sandboxes/{created['sandbox_id']}/publication")
    assert publication.status_code == 200
    assert publication.json()["remote_branch"] == "feature/add-sandbox-api"
    assert publication.json()["last_pushed_commit"] == "b" * 40
    assert publication.json()["pr_number"] == 42
    assert publication.json()["pr_merged_at"] == "2026-08-14T00:00:00Z"
    manifest = read_manifest(get_controller_store(), created["sandbox_id"])
    assert manifest is not None


def test_publish_failure_records_a_retryable_checkpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    monkeypatch.setattr(
        sandbox_service_publishing, "reviewed_target", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        sandbox_service_publishing,
        "publish_reviewed_feature",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PublishError(409, "Sandbox HEAD changed after review")
        ),
    )

    response = client.post(f"/sandboxes/{created['sandbox_id']}/publish")

    assert response.status_code == 409
    publication = get_controller_store().sandbox_publication(created["sandbox_id"])
    assert publication is not None
    assert publication["last_pushed_commit"] is None
    assert publication["last_error"] == "Sandbox HEAD changed after review"
    manifest = read_manifest(get_controller_store(), created["sandbox_id"])
    assert manifest is not None
    assert manifest.lifecycle_status == "ready"


def test_publish_push_failure_stores_a_safe_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    stderr = (
        b"remote: Permission to r-ome/personal-blog.git denied to r-ome.\n"
        b"fatal: unable to access 'https://github.com/r-ome/personal-blog/': "
        b"The requested URL returned error: 403\n"
    )
    error = ContainerError(
        container="git-push",
        exit_status=128,
        command="set -eu\ngit -C /mirror push origin",
        image="alpine/git:latest",
        stderr=stderr,
    )
    monkeypatch.setattr(
        sandbox_service_publishing, "reviewed_target", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        sandbox_service_publishing,
        "publish_reviewed_feature",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    response = client.post(f"/sandboxes/{created['sandbox_id']}/publish")

    publication = get_controller_store().sandbox_publication(created["sandbox_id"])
    assert response.status_code == 424
    assert publication is not None
    assert publication["last_error"] == (
        "GitHub rejected the write token for r-ome/personal-blog: "
        "Permission to r-ome/personal-blog.git denied to r-ome. "
        "The token usually needs the repo scope (classic) or Contents write "
        "permission (fine-grained)."
    )
    assert "/run/secrets/github_write_token" not in response.json()["detail"]


def test_publish_pr_failure_keeps_the_pushed_commit_and_retry_creates_one_pr(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    publish_calls: list[dict[str, object]] = []
    pushed_flags: list[bool] = []
    outcomes = iter(
        [
            PublishOutcome("feature/add-sandbox-api", "b" * 40, "b" * 40, True),
            PublishOutcome("feature/add-sandbox-api", "b" * 40, "b" * 40, False),
        ]
    )

    def publish(*_args: object, **kwargs: object) -> PublishOutcome:
        publish_calls.append(dict(kwargs))
        outcome = next(outcomes)
        pushed_flags.append(outcome.pushed)
        return outcome

    api_calls = 0

    def pull_request(**_kwargs: object) -> PullRequest:
        nonlocal api_calls
        api_calls += 1
        if api_calls == 1:
            raise sandbox_publish.GitHubApiError(
                "GitHub pull request creation failed (HTTP 500)"
            )
        return PullRequest(42, "https://github.com/owner/repo/pull/42", "open")

    monkeypatch.setattr(
        sandbox_service_publishing, "reviewed_target", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(sandbox_service_publishing, "publish_reviewed_feature", publish)
    monkeypatch.setattr(
        sandbox_service_publishing, "discover_or_create_pull_request", pull_request
    )

    failed = client.post(f"/sandboxes/{created['sandbox_id']}/publish")

    assert failed.status_code == 424
    publication = get_controller_store().sandbox_publication(created["sandbox_id"])
    assert publication is not None
    assert publication["last_pushed_commit"] == "b" * 40
    assert publication["remote_branch_sha"] == "b" * 40
    assert publication["pr_number"] is None
    assert publication["last_error"] == "GitHub pull request creation failed (HTTP 500)"
    failed_manifest = read_manifest(get_controller_store(), created["sandbox_id"])
    assert failed_manifest is not None

    retried = client.post(f"/sandboxes/{created['sandbox_id']}/publish")

    assert retried.status_code == 202
    assert retried.json()["pr_number"] == 42
    assert [call["remote_branch"] for call in publish_calls] == [
        "feature/add-sandbox-api",
        "feature/add-sandbox-api",
    ]
    assert pushed_flags == [True, False]
    assert api_calls == 2


def test_publish_api_failure_never_persists_or_returns_the_write_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "github-write-token-that-must-not-leak"
    created = _create(
        client,
        engine_confirmation={
            "engine": "sqlite",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    ).json()
    monkeypatch.setattr(
        sandbox_service_publishing, "reviewed_target", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        sandbox_service_publishing,
        "publish_reviewed_feature",
        lambda *_args, **_kwargs: PublishOutcome(
            "feature/add-sandbox-api", "b" * 40, "b" * 40, True
        ),
    )
    monkeypatch.setenv("ORCHESTRATOR_GITHUB_WRITE_TOKEN", token)
    monkeypatch.setattr(
        sandbox_publish.requests,
        "request",
        lambda *_args, **_kwargs: type(
            "Response",
            (),
            {"status_code": 401, "json": lambda self: {"message": token}},
        )(),
    )

    response = client.post(f"/sandboxes/{created['sandbox_id']}/publish")

    publication = get_controller_store().sandbox_publication(created["sandbox_id"])
    manifest = read_manifest(get_controller_store(), created["sandbox_id"])
    assert response.status_code == 424
    assert publication is not None and manifest is not None
    persisted = f"{publication!r} {manifest!r} {response.json()!r}"
    assert token not in persisted


def test_publish_refuses_a_migrated_legacy_sandbox(client: TestClient) -> None:
    store = get_controller_store()
    register_ready_v1_sandbox(
        store,
        sandbox_id="legacy-publish",
        project_id="legacy-project",
        project_name="legacy",
        volume_name="legacy-publish-volume",
    )
    mark_sandbox_legacy(store, "legacy-publish")

    response = client.post("/sandboxes/legacy-publish/publish")

    assert response.status_code == 409
    assert "Legacy sandbox" in response.json()["detail"]


def test_engine_routes_refuse_a_migrated_legacy_sandbox(client: TestClient) -> None:
    store = get_controller_store()
    register_ready_v1_sandbox(
        store,
        sandbox_id="legacy-engine",
        project_id="legacy-project",
        project_name="legacy",
        volume_name="legacy-volume",
    )
    mark_sandbox_legacy(store, "legacy-engine")

    response = client.post(
        "/sandboxes/legacy-engine/confirm-engine",
        json={
            "engine": "mysql",
            "migrate_commands": ["make migrate"],
            "commands_source": {"migrate": "makefile"},
            "actor": "jerome",
        },
    )

    assert response.status_code == 409
    assert client.get("/sandboxes/legacy-engine/engine").status_code == 409
