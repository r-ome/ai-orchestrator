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


def test_migration_13_keeps_one_context_per_session(tmp_path: Path) -> None:
    """A database with context revisions collapses to the ready one per session.

    Session 1 has a superseded ready revision and a newer failed one, so the
    ready row survives even though it is not the highest revision. Session 2 has
    no ready row at all, so its newest survives.
    """
    database_path = tmp_path / "controller.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE implementation_contexts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sandbox_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                manifest_json TEXT,
                commands_json TEXT,
                inventory_json TEXT,
                provider TEXT,
                model TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                settled_at TEXT
            );
            """
        )
        for version in range(1, 13):
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-01-01T00:00:00Z"),
            )
        for context_id, session_id, revision, context_status in (
            ("context-a", "session-1", 1, "ready"),
            ("context-b", "session-1", 2, "failed"),
            ("context-c", "session-2", 1, "failed"),
            ("context-d", "session-2", 2, "generating"),
        ):
            connection.execute(
                """
                INSERT INTO implementation_contexts(
                    id, session_id, sandbox_id, revision, status,
                    created_at, updated_at
                ) VALUES (?, ?, 'sandbox-1', ?, ?,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (context_id, session_id, revision, context_status),
            )
        connection.commit()
    finally:
        connection.close()

    store = ControllerStore(database_path)
    store.initialize()
    store.initialize()

    first = store.implementation_context_for_session("session-1")
    second = store.implementation_context_for_session("session-2")
    assert first is not None and first["id"] == "context-a"
    assert second is not None and second["id"] == "context-d"
    assert "revision" not in first


def test_migration_13_repoints_delegations_off_discarded_contexts(
    tmp_path: Path,
) -> None:
    """A delegation built from a superseded revision must survive the collapse.

    Foreign keys are on and `delegations.context_id` references the context
    row, so deleting a revision a delegation points at fails the whole
    migration. The delegation is repointed at its session's surviving context
    first.
    """
    database_path = tmp_path / "controller.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE implementation_contexts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sandbox_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                manifest_json TEXT,
                commands_json TEXT,
                inventory_json TEXT,
                provider TEXT,
                model TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                settled_at TEXT
            );
            CREATE TABLE delegations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sandbox_id TEXT NOT NULL,
                context_id TEXT REFERENCES implementation_contexts(id),
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                settled_at TEXT
            );
            """
        )
        for version in range(1, 13):
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-01-01T00:00:00Z"),
            )
        # Two successful generations, so both rows are ready and the newer one
        # is the survivor. The delegation was built from the older one.
        for context_id, revision in (("context-a", 1), ("context-b", 2)):
            connection.execute(
                """
                INSERT INTO implementation_contexts(
                    id, session_id, sandbox_id, revision, status,
                    created_at, updated_at
                ) VALUES (?, 'session-1', 'sandbox-1', ?, 'ready',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (context_id, revision),
            )
        connection.execute(
            """
            INSERT INTO delegations(
                id, session_id, sandbox_id, context_id, revision, status,
                created_at, updated_at
            ) VALUES ('delegation-1', 'session-1', 'sandbox-1', 'context-a', 1,
                'completed', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = ControllerStore(database_path)
    store.initialize()

    survivor = store.implementation_context_for_session("session-1")
    assert survivor is not None and survivor["id"] == "context-b"
    delegation = store.delegation("delegation-1")
    assert delegation is not None
    assert delegation["context_id"] == "context-b"
