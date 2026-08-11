import threading
from pathlib import Path

from app.controller.lifecycle import reconcile_controller_state
from app.controller.store import ControllerStore, SandboxLeaseHeldError
from app.sandboxes.lifecycle import lifecycle_lease, project_mirror_lock


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_v1_project(
        project_id="project-1", remote_url="https://example.test/repo",
        default_branch="main", mirror_volume="prj-project-mirr", created_at="",
    )
    store.register_v1_sandbox(
        sandbox_id="sandbox-1", project_id="project-1", project_name="repo",
        volume_name="sbx-sandbox-ws", created_at="",
    )
    return store


def test_mirror_lock_atomic_admission_has_exactly_one_of_32_winners(tmp_path: Path) -> None:
    store = _store(tmp_path)
    barrier = threading.Barrier(32)
    wins: list[str] = []
    guard = threading.Lock()

    def claim(index: int) -> None:
        candidate = ControllerStore(store.database_path)
        barrier.wait()
        try:
            candidate.acquire_project_mirror_lock(
                project_id="project-1", operation="create", operation_id=str(index), owner="test"
            )
        except SandboxLeaseHeldError:
            result = "blocked"
        else:
            result = "won"
        with guard:
            wins.append(result)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert wins.count("won") == 1
    assert wins.count("blocked") == 31


def test_startup_reclaims_a_stale_project_mirror_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.acquire_project_mirror_lock(
        project_id="project-1", operation="create", operation_id="stale", owner="test"
    )
    with store._connection() as connection:
        connection.execute(
            "UPDATE project_mirror_locks SET heartbeat_at = '2020-01-01T00:00:00Z'"
        )

    counts = reconcile_controller_state(store)

    assert counts["mirror_locks"] == 1
    assert store.project_mirror_lock("project-1") is None


def test_sandbox_lease_then_mirror_lock_is_acquirable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with lifecycle_lease(store, "sandbox-1", "create"):
        with project_mirror_lock(store, "project-1", "create") as lock:
            assert lock["project_id"] == "project-1"
