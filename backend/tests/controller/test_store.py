import sqlite3
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


def test_initial_migration_creates_the_current_schema_once(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

        def columns(table: str) -> set[str]:
            return {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }

        assert {"baseline_commit", "dirty_baseline_json"} <= columns("sandboxes")
        assert {"kind", "task_id", "commit_sha"} <= columns("preview_runs")
        assert {"base_branch", "baseline_dirty_json"} <= columns("tasks")
        assert "model" in columns("planning_messages")
        assert "revision" not in columns("implementation_contexts")
        assert {
            "base_branch",
            "base_commit",
            "head_commit",
            "source_merged_at",
        } <= columns("delegation_reviews")
        assert {"routing_source", "turn_finished_at"} <= columns("work_item_runs")
        assert "prompt" in columns("delegation_change_requests")

    assert versions == [1]
    assert {
        "tasks",
        "planning_sessions",
        "implementation_contexts",
        "delegations",
        "delegation_reviews",
        "delegation_change_requests",
        "work_items",
        "work_item_runs",
    } <= tables
    assert "one_context_per_session" in indexes
