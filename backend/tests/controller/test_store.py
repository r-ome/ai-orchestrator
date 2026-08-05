from pathlib import Path

from app.controller.store import ControllerStore


def test_register_sandbox_retires_stale_volume_name_owner(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    shared = {
        "project_id": "project-1",
        "project_name": "sample-sandbox-1",
        "source_path": "/projects/sample",
        "volume_name": "orchestrator-project-sample-sandbox-1",
        "status": "ready",
        "created_at": "2026-08-04T00:00:00Z",
    }
    store.register_sandbox(sandbox_id="sandbox-old", **shared)

    store.register_sandbox(sandbox_id="sandbox-current", **shared)
    store.register_sandbox(sandbox_id="sandbox-current", **shared)

    sandboxes = {row["id"]: row for row in store.sandboxes()}
    assert len(sandboxes) == 2
    assert sandboxes["sandbox-old"]["volume_name"] == (
        "orchestrator-project-sample-sandbox-1#retired:sandbox-old"
    )
    assert sandboxes["sandbox-old"]["status"] == "missing"
    assert sandboxes["sandbox-current"]["volume_name"] == shared["volume_name"]
    assert sandboxes["sandbox-current"]["status"] == "ready"
