import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeDockerClient, register_ready_v1_sandbox
from docker.errors import DockerException

from app.controller.store import ControllerStore
from app.delegation import service as delegation_service
from app.platform.naming import ownership_labels
from app.startup import _settle_interrupted_turns, reconcile_controller_state

PLAN = {
    "title": "Feature",
    "scope": "Implement the feature",
    "approach": "Add the behavior and tests",
}


def _item(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Item {key}",
        "objective": "do it",
        "scope": "src/app.py",
        "dependencies": [],
        "files": ["src/app.py"],
        "write_scope": ["src/app.py"],
        "acceptance_criteria": ["behavior works"],
        "verification": [{"command_kind": "test"}],
        "complexity": "medium",
    }


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="sample-volume",
        created_at="2026-08-08T00:00:00Z",
    )
    store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Sample plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    return store


def test_a_generating_context_is_failed_so_its_session_is_not_blocked(
    tmp_path: Path,
) -> None:
    """A killed process leaves the row claimed and the unique index armed.

    `one_generating_context_per_session` cannot tell a live turn from one whose
    thread died with the interpreter, so without this the session answers every
    later request with 409 forever.
    """
    store = _store(tmp_path)
    store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "test-model",
        }
    )

    settled, abandoned = _settle_interrupted_turns(store)

    assert settled == 1
    assert abandoned == []
    row = store.implementation_context("context-1")
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] == "The backend restarted while this turn was running"
    # The index is free again, so the next revision can be claimed.
    store.start_implementation_context(
        {
            "id": "context-2",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "revision": 2,
            "status": "generating",
            "provider": "claude",
            "model": "test-model",
        }
    )


def test_settled_rows_are_left_alone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "test-model",
        }
    )
    store.settle_implementation_context("context-1", to_status="ready")

    assert _settle_interrupted_turns(store) == (0, [])

    row = store.implementation_context("context-1")
    assert row is not None
    assert row["status"] == "ready"
    assert not row["error"]


def _run(store: ControllerStore, task_id: str) -> str:
    """A running work item run on a task, as `claim_run` leaves it."""
    store.set_plan_spec(session_id="session-1", plan_spec=PLAN)
    store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "test-model",
        }
    )
    store.settle_implementation_context(
        "context-1",
        to_status="ready",
        changes={
            "manifest_json": "{}",
            "commands_json": json.dumps(
                [
                    {
                        "kind": "test",
                        "command": "pytest",
                        "confirmed": True,
                        "reason": "defined",
                    }
                ]
            ),
        },
    )
    view = delegation_service.create_revision(store, "session-1", [_item("a")])
    store.create_task(
        task_id=task_id,
        sandbox_id="sandbox-1",
        agent_run_id=None,
        branch=f"task/{task_id}",
        base_branch="main",
        base_commit="0" * 40,
        title="Item a",
        status="review",
    )
    store.claim_work_item_run(
        {
            "id": "run-1",
            "work_item_id": view.items[0].item.id,
            "delegation_id": view.delegation.id,
            "status": "running",
            "provider": "claude",
            "model": "test-model",
            "task_id": task_id,
        }
    )
    return view.delegation.id


def test_an_interrupted_run_hands_back_the_task_it_abandoned(
    tmp_path: Path,
) -> None:
    """Failing the run is not enough: its task holds the sandbox's only slot.

    `start_task` allows one open task per sandbox, and an interrupted run
    leaves its task in 'review'. Without handing the id back for rejection,
    every later run on that sandbox is refused with 409.
    """
    store = _store(tmp_path)
    _run(store, "task-1")

    settled, abandoned = _settle_interrupted_turns(store)

    assert settled == 1
    assert abandoned == ["task-1"]
    row = store.work_item_run("run-1")
    assert row is not None
    assert row["status"] == "failed"


def test_a_run_awaiting_a_decision_survives_a_restart(tmp_path: Path) -> None:
    """It is 'running' on purpose, holding a verified commit for a person.

    Reconciliation must not fail it, and must not hand its task back to be
    rejected — that would delete the branch the commit lives on.
    """
    store = _store(tmp_path)
    _run(store, "task-1")
    store.finish_work_item_turn("run-1")

    assert _settle_interrupted_turns(store) == (0, [])

    row = store.work_item_run("run-1")
    assert row is not None
    assert row["status"] == "running"
    assert row["turn_finished_at"]
    task = store.task("task-1")
    assert task is not None
    assert task["status"] == "review"


def test_startup_closes_every_open_agent_writer_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    store.start_agent_run(
        run_id="agent-1",
        sandbox_id="sandbox-1",
        provider="claude",
        container_id="container-1",
        status="running",
    )
    store.open_agent_writer_session(
        session_id="writer-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        kind="terminal",
    )

    class UnavailableDocker:
        @staticmethod
        def from_env():
            from docker.errors import DockerException

            raise DockerException("unavailable")

    monkeypatch.setattr("app.startup.docker", UnavailableDocker)

    counts = reconcile_controller_state(store)

    assert counts["writer_sessions"] == 1
    session = store.agent_writer_session("writer-1")
    assert session is not None
    assert session["ended_at"] is not None
    assert store.active_writers("sandbox-1") == []


def test_startup_reclaims_a_lease_for_a_settled_operation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes
            SET lifecycle_version = 'v1', desired_state = 'active',
                lifecycle_status = 'ready'
            WHERE id = 'sandbox-1'
            """
        )
    store.acquire_sandbox_lease(
        sandbox_id="sandbox-1",
        operation="sync",
        operation_id="sync-1",
        owner="dead-process",
    )

    class UnavailableDocker:
        @staticmethod
        def from_env():
            from docker.errors import DockerException

            raise DockerException("unavailable")

    monkeypatch.setattr("app.startup.docker", UnavailableDocker)

    counts = reconcile_controller_state(store)

    assert counts["leases"] == 1
    assert store.sandbox_lease("sandbox-1") is None
    store.create_task(
        task_id="task-after-reclaim",
        sandbox_id="sandbox-1",
        agent_run_id=None,
        branch="task/after-reclaim",
        base_branch="",
        base_commit="",
        title="usable",
        status="preparing",
    )


def test_startup_reclaims_a_stale_unsettled_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes
            SET lifecycle_version = 'v1', desired_state = 'active',
                lifecycle_status = 'ready'
            WHERE id = 'sandbox-1'
            """
        )
    store.acquire_sandbox_lease(
        sandbox_id="sandbox-1",
        operation="sync",
        operation_id="sync-1",
        owner="dead-process",
    )
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes SET lifecycle_status = 'syncing'
            WHERE id = 'sandbox-1'
            """
        )
        connection.execute(
            """
            UPDATE sandbox_leases SET heartbeat_at = '2020-01-01T00:00:00Z'
            WHERE sandbox_id = 'sandbox-1'
            """
        )

    class UnavailableDocker:
        @staticmethod
        def from_env():
            from docker.errors import DockerException

            raise DockerException("unavailable")

    monkeypatch.setattr("app.startup.docker", UnavailableDocker)

    counts = reconcile_controller_state(store)

    assert counts["leases"] == 1
    assert store.sandbox_lease("sandbox-1") is None
    with store._connection() as connection:
        connection.execute(
            "UPDATE sandboxes SET lifecycle_status = 'ready' WHERE id = 'sandbox-1'"
        )
    store.start_agent_run(
        run_id="agent-after-reclaim",
        sandbox_id="sandbox-1",
        provider="claude",
        status="created",
    )


def test_startup_reports_each_unclaimed_sbx_resource_without_removing_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    docker_client = FakeDockerClient()
    orphan_id = "a" * 32
    labels = ownership_labels(sandbox_id=orphan_id, project_id="orphan-project")
    volume = docker_client.volumes.create(name="sbx-aaaaaaaaaaaa-volume", labels=labels)
    container = docker_client.containers.create(
        name="sbx-aaaaaaaaaaaa-container", image="test", labels=labels
    )
    network = docker_client.networks.create("sbx-aaaaaaaaaaaa-network", labels=labels)

    monkeypatch.setattr(
        "app.startup.docker.from_env", lambda: docker_client
    )
    monkeypatch.setattr(
        store,
        "acquire_sandbox_lease",
        lambda **_kwargs: pytest.fail("orphan reporting must not acquire a lifecycle lease"),
    )
    monkeypatch.setattr(
        store,
        "acquire_project_mirror_lock",
        lambda **_kwargs: pytest.fail("orphan reporting must not acquire a mirror lock"),
    )
    counts = reconcile_controller_state(store)

    assert counts["orphan_resources"] == 3
    assert volume.removed is container.removed is network.removed is False
    assert {resource["resource"] for resource in store.unexpected_resources()} == {
        "volume:sbx-aaaaaaaaaaaa-volume",
        "container:sbx-aaaaaaaaaaaa-container",
        "network:sbx-aaaaaaaaaaaa-network",
    }


def test_startup_orphan_reporting_skips_manifest_claims_and_shared_infrastructure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    docker_client = FakeDockerClient()
    claimed = docker_client.volumes.create(
        name="sbx-claimed-volume",
        labels=ownership_labels(sandbox_id="b" * 32, project_id="project-1"),
    )
    store.record_sandbox_resource("sandbox-1", kind="volume", name=claimed.name)
    mirror = docker_client.volumes.create(
        name="sbx-mirror-looking",
        labels={"orchestrator.project.mirror": "true"},
    )
    shared_database = docker_client.containers.create(
        name="sbx-shared-database-looking",
        image="test",
        labels={"orchestrator.shared-database": "true"},
    )
    shared_data = docker_client.volumes.create(
        name="sbx-shared-database-data-looking",
        labels={"orchestrator.shared-database": "true"},
    )
    shared_credentials = docker_client.volumes.create(
        name="sbx-shared-database-credentials-looking",
        labels={"orchestrator.shared-database": "true"},
    )
    shared_network = docker_client.networks.create(
        "sbx-shared-database-network-looking",
        labels={"orchestrator.shared-database": "true"},
    )
    dependency = docker_client.volumes.create(
        name="sbx-dependency-looking",
        labels={
            "orchestrator.preview.data-managed": "true",
            "orchestrator.preview.persistent": "true",
        },
    )

    monkeypatch.setattr(
        "app.startup.docker.from_env", lambda: docker_client
    )
    counts = reconcile_controller_state(store)

    assert counts["orphan_resources"] == 0
    assert store.unexpected_resources() == []
    assert (
        claimed.removed
        is mirror.removed
        is shared_database.removed
        is shared_data.removed
        is shared_credentials.removed
        is shared_network.removed
        is dependency.removed
        is False
    )


def test_startup_orphan_reporting_degrades_when_docker_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)

    class UnavailableDocker:
        @staticmethod
        def from_env():
            from docker.errors import DockerException

            raise DockerException("unavailable")

    monkeypatch.setattr("app.startup.docker", UnavailableDocker)

    counts = reconcile_controller_state(store)

    assert counts["orphan_resources"] == 0
    assert counts["orphan_resource_failures"] == 0


def test_startup_orphan_reporting_keeps_partial_findings_after_a_docker_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    docker_client = FakeDockerClient()
    labels = ownership_labels(sandbox_id="c" * 32, project_id="orphan-project")
    docker_client.volumes.create(name="sbx-cccccccccccc-volume", labels=labels)
    docker_client.containers.create(
        name="sbx-cccccccccccc-container", image="test", labels=labels
    )
    docker_client.inject_failure("networks.list", DockerException("unavailable"))
    monkeypatch.setattr(
        "app.startup.docker.from_env", lambda: docker_client
    )

    counts = reconcile_controller_state(store)

    assert counts["orphan_resources"] == 2
    assert counts["orphan_resource_failures"] == 1
