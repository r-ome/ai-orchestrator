import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import register_ready_v1_sandbox

from app.controller.store import (
    ActiveAgentRunExists,
    AgentWriterSessionExists,
    ControllerStore,
    OpenTaskExists,
    SandboxLeaseBlockedByWriterError,
    SandboxLeaseHeldError,
    SandboxWriterAdmissionError,
    SlotTaken,
)
from app.sandboxes import lifecycle as sandbox_lifecycle


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="sample-volume",
        created_at="2026-08-11T00:00:00Z",
    )
    return store


def _agent(store: ControllerStore) -> None:
    store.start_agent_run(
        run_id="agent-1",
        sandbox_id="sandbox-1",
        provider="claude",
        container_id="container-1",
        status="running",
    )


def _managed_ready(store: ControllerStore, sandbox_id: str = "sandbox-1") -> None:
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes
            SET lifecycle_version = 'v1', desired_state = 'active',
                lifecycle_status = 'ready'
            WHERE id = ?
            """,
            (sandbox_id,),
        )


def _preview_values(sandbox_id: str, preview_id: str = "preview-1") -> dict[str, Any]:
    return {
        "id": preview_id,
        "sandbox_id": sandbox_id,
        "proposal_id": f"proposal-{preview_id}",
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


def test_idle_main_agent_environment_is_not_an_active_writer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _agent(store)

    assert store.active_agent("sandbox-1") is not None
    assert store.active_writers("sandbox-1") == []


def test_open_agent_terminal_is_a_writer_until_it_detaches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _agent(store)
    store.open_agent_writer_session(
        session_id="session-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        kind="terminal",
    )

    assert store.active_writers("sandbox-1") == [
        {
            "writer_class": "agent_writer_session",
            "writer_id": "session-1",
            "status": "open",
            "kind": "terminal",
            "agent_run_id": "agent-1",
        }
    ]

    assert store.close_agent_writer_session("session-1")
    assert store.active_writers("sandbox-1") == []


def test_unique_index_rejects_two_open_sessions_for_one_sandbox(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _agent(store)
    store.open_agent_writer_session(
        session_id="session-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        kind="terminal",
    )

    with pytest.raises(AgentWriterSessionExists):
        store.open_agent_writer_session(
            session_id="session-2",
            sandbox_id="sandbox-1",
            agent_run_id="agent-1",
            kind="terminal",
        )


def test_unique_index_rejects_two_open_tasks_for_one_sandbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_task(
        task_id="task-1",
        sandbox_id="sandbox-1",
        agent_run_id=None,
        branch="task/task-1",
        base_branch="main",
        base_commit="a" * 40,
        title="first",
        status="open",
    )

    with pytest.raises(OpenTaskExists):
        store.create_task(
            task_id="task-2",
            sandbox_id="sandbox-1",
            agent_run_id=None,
            branch="task/task-2",
            base_branch="main",
            base_commit="a" * 40,
            title="second",
            status="open",
        )


def test_unique_index_rejects_two_active_agent_runs_for_one_sandbox(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _agent(store)

    with pytest.raises(ActiveAgentRunExists):
        store.start_agent_run(
            run_id="agent-2",
            sandbox_id="sandbox-1",
            provider="claude",
            container_id="container-2",
            status="running",
        )


def test_task_primary_key_conflict_stays_a_sqlite_integrity_error(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-2",
        project_id="project-2",
        project_name="sample-two",
        volume_name="sample-two-volume",
        created_at="2026-08-11T00:00:00Z",
    )
    store.create_task(
        task_id="task-1",
        sandbox_id="sandbox-1",
        agent_run_id=None,
        branch="task/task-1",
        base_branch="main",
        base_commit="a" * 40,
        title="first",
        status="open",
    )

    with pytest.raises(sqlite3.IntegrityError) as caught:
        store.create_task(
            task_id="task-1",
            sandbox_id="sandbox-2",
            agent_run_id=None,
            branch="task/task-2",
            base_branch="main",
            base_commit="a" * 40,
            title="second",
            status="open",
        )

    assert not isinstance(caught.value, SlotTaken)


def test_writer_detection_names_each_blocking_class(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _agent(store)
    store.open_agent_writer_session(
        session_id="session-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        kind="terminal",
    )
    store.create_task(
        task_id="task-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        branch="task/task-1",
        base_branch="",
        base_commit="",
        title="sample",
        status="preparing",
    )
    store.create_preview_run(
        {
            "id": "preview-1",
            "sandbox_id": "sandbox-1",
            "proposal_id": "proposal-1",
            "mode": "native",
            "kind": "live",
            "task_id": None,
            "commit_sha": None,
            "status": "preparing",
            "selected_service": None,
            "container_port": 3000,
            "host_port": 43000,
            "config_json": "{}",
            "config_digest": "digest",
            "network_name": None,
            "created_at": "2026-08-11T00:00:00Z",
            "started_at": None,
            "expires_at": None,
            "last_activity_at": "2026-08-11T00:00:00Z",
        }
    )
    store.create_planning_session(
        session_id="planning-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    with store._connection() as connection:
        connection.execute(
            """
            INSERT INTO delegations(
                id, session_id, sandbox_id, revision, status, created_at, updated_at
            ) VALUES (
                'delegation-1', 'planning-1', 'sandbox-1', 1, 'ready',
                '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z'
            )
            """
        )

    writers = store.active_writers("sandbox-1")

    assert {writer["writer_class"] for writer in writers} == {
        "task",
        "preview",
        "delegation",
        "agent_writer_session",
    }
    assert {writer["writer_id"] for writer in writers} == {
        "task-1",
        "preview-1",
        "delegation-1",
        "session-1",
    }


def test_idle_main_agent_does_not_block_lifecycle_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    _agent(store)

    with store.sandbox_lifecycle_lease(
        sandbox_id="sandbox-1",
        operation="sync",
        operation_id="sync-1",
        owner="test",
    ) as lease:
        assert lease is not None
        assert lease["operation"] == "sync"

    assert store.sandbox_lease("sandbox-1") is None


def test_open_agent_writer_blocks_lease_and_error_names_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    _agent(store)
    store.open_agent_writer_session(
        session_id="writer-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        kind="terminal",
    )

    with pytest.raises(SandboxLeaseBlockedByWriterError) as caught:
        store.acquire_sandbox_lease(
            sandbox_id="sandbox-1",
            operation="sync",
            operation_id="sync-1",
            owner="test",
        )

    assert caught.value.writer_class == "agent_writer_session"
    assert caught.value.writer_id == "writer-1"
    assert "agent_writer_session" in str(caught.value)
    assert "writer-1" in str(caught.value)


def test_lease_excludes_a_second_lifecycle_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    store.acquire_sandbox_lease(
        sandbox_id="sandbox-1",
        operation="sync",
        operation_id="sync-1",
        owner="first",
    )

    with pytest.raises(SandboxLeaseHeldError) as caught:
        store.acquire_sandbox_lease(
            sandbox_id="sandbox-1",
            operation="publish",
            operation_id="publish-1",
            owner="second",
        )

    assert caught.value.lease["operation"] == "sync"
    assert caught.value.lease["operation_id"] == "sync-1"


def test_lease_blocks_every_writer_start_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    _agent(store)
    store.create_planning_session(
        session_id="planning-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.acquire_sandbox_lease(
        sandbox_id="sandbox-1",
        operation="sync",
        operation_id="sync-1",
        owner="test",
    )

    with pytest.raises(SandboxWriterAdmissionError):
        store.create_task(
            task_id="task-1",
            sandbox_id="sandbox-1",
            agent_run_id="agent-1",
            branch="task/task-1",
            base_branch="",
            base_commit="",
            title="sample",
            status="preparing",
        )
    with pytest.raises(SandboxWriterAdmissionError):
        store.start_agent_run(
            run_id="agent-2",
            sandbox_id="sandbox-1",
            provider="codex",
        )
    with pytest.raises(SandboxWriterAdmissionError):
        store.create_preview_run(_preview_values("sandbox-1"))
    with pytest.raises(SandboxWriterAdmissionError):
        store.claim_delegation_revision(
            {
                "id": "delegation-1",
                "session_id": "planning-1",
                "sandbox_id": "sandbox-1",
                "context_id": None,
                "status": "ready",
            },
            [],
        )
    with pytest.raises(SandboxWriterAdmissionError):
        store.open_agent_writer_session(
            session_id="writer-1",
            sandbox_id="sandbox-1",
            agent_run_id="agent-1",
            kind="terminal",
        )

    assert store.active_writers("sandbox-1") == []


def test_engine_confirmation_names_the_action_that_unblocks_delegation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create_planning_session(
        session_id="planning-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes
            SET lifecycle_version = 'v1', desired_state = 'active',
                lifecycle_status = 'awaiting_engine_confirmation'
            WHERE id = 'sandbox-1'
            """
        )

    with pytest.raises(SandboxWriterAdmissionError) as caught:
        store.claim_delegation_revision(
            {
                "id": "delegation-1",
                "session_id": "planning-1",
                "sandbox_id": "sandbox-1",
                "context_id": None,
                "status": "ready",
            },
            [],
        )

    assert "confirm the database engine" in str(caught.value)


def test_lifecycle_lease_refuses_a_new_delegation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    store.create_planning_session(
        session_id="planning-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.acquire_sandbox_lease(
        sandbox_id="sandbox-1",
        operation="sync",
        operation_id="sync-1",
        owner="test",
    )

    with pytest.raises(SandboxWriterAdmissionError) as caught:
        store.claim_delegation_revision(
            {
                "id": "delegation-1",
                "session_id": "planning-1",
                "sandbox_id": "sandbox-1",
                "context_id": None,
                "status": "ready",
            },
            [],
        )

    assert "lifecycle operation sync 'sync-1'" in str(caught.value)


def test_two_concurrent_lifecycle_starts_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    first = ControllerStore(store.database_path)
    second = ControllerStore(store.database_path)
    barrier = threading.Barrier(2)
    result_lock = threading.Lock()
    results: list[str] = []

    def claim(candidate: ControllerStore, operation_id: str) -> None:
        barrier.wait()
        try:
            candidate.acquire_sandbox_lease(
                sandbox_id="sandbox-1",
                operation="sync",
                operation_id=operation_id,
                owner=operation_id,
            )
        except SandboxLeaseHeldError:
            result = "blocked"
        else:
            result = "acquired"
        with result_lock:
            results.append(result)

    threads = [
        threading.Thread(target=claim, args=(first, "sync-1")),
        threading.Thread(target=claim, args=(second, "sync-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results.count("acquired") == 1
    assert results.count("blocked") == 1


def test_writer_and_lease_admission_cannot_both_win(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    lease_store = ControllerStore(store.database_path)
    writer_store = ControllerStore(store.database_path)
    barrier = threading.Barrier(2)
    result_lock = threading.Lock()
    results: list[str] = []

    def claim_lease() -> None:
        barrier.wait()
        try:
            lease_store.acquire_sandbox_lease(
                sandbox_id="sandbox-1",
                operation="sync",
                operation_id="sync-1",
                owner="lease",
            )
        except SandboxLeaseBlockedByWriterError:
            result = "lease-blocked"
        else:
            result = "lease-acquired"
        with result_lock:
            results.append(result)

    def start_writer() -> None:
        barrier.wait()
        try:
            writer_store.create_task(
                task_id="task-1",
                sandbox_id="sandbox-1",
                agent_run_id=None,
                branch="task/task-1",
                base_branch="",
                base_commit="",
                title="sample",
                status="preparing",
            )
        except SandboxWriterAdmissionError:
            result = "writer-blocked"
        else:
            result = "writer-acquired"
        with result_lock:
            results.append(result)

    threads = [
        threading.Thread(target=claim_lease),
        threading.Thread(target=start_writer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) in (
        ["lease-acquired", "writer-blocked"],
        ["lease-blocked", "writer-acquired"],
    )
    assert not (
        store.sandbox_lease("sandbox-1") is not None
        and store.active_writers("sandbox-1")
    )


def test_delegation_task_and_preview_writer_rows_can_nest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    store.create_planning_session(
        session_id="planning-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="plan",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    store.claim_delegation_revision(
        {
            "id": "delegation-1",
            "session_id": "planning-1",
            "sandbox_id": "sandbox-1",
            "context_id": None,
            "status": "ready",
        },
        [],
    )
    store.create_task(
        task_id="task-1",
        sandbox_id="sandbox-1",
        agent_run_id=None,
        branch="task/task-1",
        base_branch="main",
        base_commit="0" * 40,
        title="sample",
        status="review",
    )
    store.create_preview_run(
        {
            **_preview_values("sandbox-1"),
            "task_id": "task-1",
            "commit_sha": "0" * 40,
        }
    )

    assert {writer["writer_class"] for writer in store.active_writers("sandbox-1")} == {
        "delegation",
        "task",
        "preview",
    }


def test_destroy_claims_with_writer_and_blocks_new_writers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    store.start_agent_run(
        run_id="agent-1",
        sandbox_id="sandbox-1",
        provider="claude",
        container_id=None,
        status="running",
    )
    store.open_agent_writer_session(
        session_id="writer-1",
        sandbox_id="sandbox-1",
        agent_run_id="agent-1",
        kind="terminal",
    )

    with store.sandbox_lifecycle_lease(
        sandbox_id="sandbox-1",
        operation="destroy",
        operation_id="destroy-1",
        owner="test",
        allow_writers=True,
    ):
        sandbox = store.sandbox("sandbox-1")
        assert sandbox is not None
        assert sandbox["desired_state"] == "destroyed"
        assert sandbox["lifecycle_status"] == "draining"
        with pytest.raises(SandboxWriterAdmissionError):
            store.create_task(
                task_id="task-1",
                sandbox_id="sandbox-1",
                agent_run_id=None,
                branch="task/task-1",
                base_branch="",
                base_commit="",
                title="sample",
                status="preparing",
            )
        assert (
            sandbox_lifecycle.drain_sandbox_writers(
                object(),
                store,
                "sandbox-1",
            )
            == 1
        )
        assert store.active_writers("sandbox-1") == []

    assert store.sandbox_lease("sandbox-1") is None


def test_preview_stop_is_explicit_and_then_retries_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _managed_ready(store)
    store.create_preview_run(_preview_values("sandbox-1"))
    stopped: list[str] = []

    def stop_preview(*args, preview_id: str, **kwargs) -> None:
        del args, kwargs
        stopped.append(preview_id)
        store.update_preview_run(preview_id, status="stopped")

    monkeypatch.setattr(sandbox_lifecycle, "_stop_blocking_preview", stop_preview)

    with pytest.raises(SandboxLeaseBlockedByWriterError):
        with sandbox_lifecycle.lifecycle_lease(store, "sandbox-1", "sync"):
            pass
    assert stopped == []

    with sandbox_lifecycle.lifecycle_lease(
        store,
        "sandbox-1",
        "sync",
        docker_client=object(),
        stop_blocking_previews=True,
    ) as lease:
        assert lease is not None
        assert stopped == ["preview-1"]
