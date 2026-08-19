import sqlite3
from collections.abc import Callable, Mapping

from ._shared import _now


def _violates(error: sqlite3.IntegrityError, index: str) -> bool:
    message = str(error)
    if message == f"UNIQUE constraint failed: index '{index}'":
        return True
    # Some SQLite builds name the indexed column for partial index conflicts.
    # Keep this fallback exact, so another constraint failure still escapes as
    # its original sqlite3.IntegrityError.
    return message == {
        "one_open_task_per_sandbox": "UNIQUE constraint failed: tasks.sandbox_id",
        "one_active_agent_per_sandbox": "UNIQUE constraint failed: agent_runs.sandbox_id",
        "one_open_agent_writer_session_per_sandbox": (
            "UNIQUE constraint failed: agent_writer_sessions.sandbox_id"
        ),
        "one_delegation_revision_per_session": (
            "UNIQUE constraint failed: delegations.session_id, delegations.revision"
        ),
        "one_active_delegation_per_sandbox": (
            "UNIQUE constraint failed: delegations.sandbox_id"
        ),
        "one_review_revision_per_delegation": (
            "UNIQUE constraint failed: delegation_reviews.delegation_id, "
            "delegation_reviews.revision"
        ),
        "one_generating_review_per_delegation": (
            "UNIQUE constraint failed: delegation_reviews.delegation_id"
        ),
        "one_change_revision_per_delegation": (
            "UNIQUE constraint failed: delegation_change_requests.delegation_id, "
            "delegation_change_requests.revision"
        ),
        "one_running_change_per_delegation": (
            "UNIQUE constraint failed: delegation_change_requests.delegation_id"
        ),
        "one_attempt_number_per_work_item": (
            "UNIQUE constraint failed: work_item_runs.work_item_id, "
            "work_item_runs.attempt"
        ),
        "one_running_run_per_delegation": (
            "UNIQUE constraint failed: work_item_runs.delegation_id"
        ),
    }.get(index)


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as error:
        if "duplicate column name" not in str(error):
            raise


def _add_sandbox_lifecycle_columns(connection: sqlite3.Connection) -> None:
    for column, ddl in (
        ("lifecycle_version", "TEXT"),
        ("feature_key", "TEXT"),
        ("feature_title", "TEXT"),
        ("desired_state", "TEXT"),
        ("lifecycle_status", "TEXT"),
        ("last_error", "TEXT"),
        ("base_ref", "TEXT"),
        ("created_base_commit", "TEXT"),
        ("current_base_commit", "TEXT"),
        ("pending_base_commit", "TEXT"),
        ("feature_branch", "TEXT"),
        ("agent_provider", "TEXT"),
        ("network_policy", "TEXT"),
        ("db_engine", "TEXT"),
        ("db_name", "TEXT"),
        ("schema_baseline_hash", "TEXT"),
        ("db_data_volume", "TEXT"),
        ("publish_remote", "TEXT"),
        ("remote_branch", "TEXT"),
        ("pr_requested", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(connection, "sandboxes", column, ddl)


def _backfill_legacy_sandboxes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE sandboxes
        SET lifecycle_version = 'legacy', desired_state = 'active'
        WHERE lifecycle_version IS NULL
        """
    )


def _rebuild_projects_table(connection: sqlite3.Connection) -> None:
    """Makes project paths nullable while preserving sandbox foreign keys."""
    if connection.in_transaction:
        raise RuntimeError("projects rebuild must start in autocommit mode")

    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise RuntimeError("could not disable foreign keys for projects rebuild")

    try:
        connection.execute("BEGIN")
        connection.execute(
            """
            CREATE TABLE projects_new (
                id TEXT PRIMARY KEY,
                source_path TEXT,
                remote_url TEXT,
                default_branch TEXT,
                mirror_volume TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO projects_new(id, source_path, created_at)
            SELECT id, source_path, created_at FROM projects
            """
        )
        connection.execute("DROP TABLE projects")
        connection.execute("ALTER TABLE projects_new RENAME TO projects")
        connection.execute(
            """
            CREATE UNIQUE INDEX projects_source_path ON projects(source_path)
            WHERE source_path IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX projects_remote_url ON projects(remote_url)
            WHERE remote_url IS NOT NULL
            """
        )
        connection.commit()

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"projects rebuild left foreign key violations: {violations!r}"
            )
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def _create_agent_writer_sessions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_writer_sessions (
            id TEXT PRIMARY KEY,
            sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
            agent_run_id TEXT NOT NULL REFERENCES agent_runs(id),
            kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            heartbeat_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_agent_writer_session_per_sandbox
        ON agent_writer_sessions(sandbox_id)
        WHERE ended_at IS NULL
        """
    )


def _create_sandbox_leases(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_leases (
            sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS sandbox_leases_by_heartbeat
        ON sandbox_leases(heartbeat_at)
        """
    )


def _include_preparing_in_open_tasks(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS one_open_task_per_sandbox")
    connection.execute(
        """
        CREATE UNIQUE INDEX one_open_task_per_sandbox
        ON tasks(sandbox_id)
        WHERE status IN ('preparing', 'open', 'reported', 'previewing', 'review')
        """
    )


def _create_phase_5c_5d_tables(connection: sqlite3.Connection) -> None:
    """Create the project mirror mutex and durable destroy receipts.

    Lock order is deliberately not encoded in SQLite: callers must acquire a
    sandbox lease first and this project lock second.  The two rows have
    different scopes, and this fixed order keeps a create or sync deadlock
    free.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS project_mirror_locks (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            operation TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )
        """
    )


    connection.execute(
        "CREATE INDEX IF NOT EXISTS project_mirror_locks_by_heartbeat "
        "ON project_mirror_locks(heartbeat_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_tombstones (
            sandbox_id TEXT PRIMARY KEY,
            destroyed_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        )
        """
    )
    # The manifest records exact resources at creation time.  Destroy never
    # searches Docker names or labels to expand this list.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_resources (
            sandbox_id TEXT NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY(sandbox_id, kind, name)
        )
        """
    )


def _create_sandbox_engine_detections(connection: sqlite3.Connection) -> None:
    """Store a reviewable engine proposal and its approved command snapshot."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_engine_detections (
            sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id) ON DELETE CASCADE,
            signals_json TEXT NOT NULL,
            proposed_engine TEXT,
            confirmed_engine TEXT,
            migrate_commands_json TEXT NOT NULL,
            seed_commands_json TEXT NOT NULL,
            commands_source TEXT NOT NULL,
            detected_at_commit TEXT NOT NULL,
            actor TEXT,
            confirmed_at TEXT
        )
        """
    )


def _create_sandbox_databases(connection: sqlite3.Connection) -> None:
    """Store only sandbox-scoped application credentials and provision state."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_databases (
            sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id) ON DELETE CASCADE,
            engine TEXT NOT NULL,
            db_name TEXT NOT NULL,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            status TEXT NOT NULL,
            provisioned_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _add_project_mirror_fetched_at(connection: sqlite3.Connection) -> None:
    """Record when the controller last fetched each shared project mirror."""
    _add_column(connection, "projects", "mirror_fetched_at", "TEXT")


def _add_publication_merged_at(connection: sqlite3.Connection) -> None:
    """Record when GitHub reports that a pull request merged."""
    _add_column(connection, "sandbox_publications", "pr_merged_at", "TEXT")


def _add_publication_session_id(connection: sqlite3.Connection) -> None:
    """Name the planning session a publication belongs to.

    The row is keyed by sandbox, so a sandbox holding more than one planning
    session handed the same pull request to every one of them: a session that
    had reached its review limit without producing a plan still read as
    `published`, because its neighbour had an open PR.

    The backfill attributes an existing row to the session whose delegation
    settled most recently, which is the only session in that sandbox that can
    have produced the pushed branch. A sandbox with no delegation at all keeps
    NULL, and NULL attributes the publication to nobody rather than guessing.
    """
    # A database that stamped migration 28 without running it has no table to
    # alter. Creating it here is idempotent and leaves the column reachable
    # either way.
    _create_sandbox_publications(connection)
    _add_column(connection, "sandbox_publications", "session_id", "TEXT")
    connection.execute(
        """
        UPDATE sandbox_publications
        SET session_id = (
            SELECT delegation.session_id
            FROM delegations AS delegation
            JOIN planning_sessions AS session
                ON session.id = delegation.session_id
            WHERE session.sandbox_id = sandbox_publications.sandbox_id
            ORDER BY COALESCE(delegation.settled_at, delegation.updated_at) DESC
            LIMIT 1
        )
        WHERE session_id IS NULL
        """
    )


def _add_planning_session_models(connection: sqlite3.Connection) -> None:
    """Record the provider model each planning role uses."""
    for column in (
        "clarifier_model",
        "planner_model",
        "reviewer_model",
        "reviewer_reasoning_effort",
    ):
        _add_column(connection, "planning_sessions", column, "TEXT")


def _create_sandbox_publications(connection: sqlite3.Connection) -> None:
    """Record observed Git publication facts, never publication intent."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_publications (
            sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id) ON DELETE CASCADE,
            remote_branch TEXT NOT NULL,
            last_pushed_commit TEXT,
            remote_branch_sha TEXT,
            pr_number INTEGER,
            pr_url TEXT,
            pr_state TEXT,
            pr_merged_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


MIGRATIONS: Mapping[int, Callable[[sqlite3.Connection], None]] = {
    18: _add_sandbox_lifecycle_columns,
    19: _backfill_legacy_sandboxes,
    20: _rebuild_projects_table,
    21: _create_sandbox_leases,
    22: _create_agent_writer_sessions,
    23: _include_preparing_in_open_tasks,
    24: _create_phase_5c_5d_tables,
    25: _create_sandbox_engine_detections,
    26: _create_sandbox_databases,
    27: _add_project_mirror_fetched_at,
    28: _create_sandbox_publications,
    29: _add_publication_merged_at,
    30: _add_planning_session_models,
    31: _add_publication_session_id,
}


def _apply_migrations(connection: sqlite3.Connection) -> None:
    applied = {
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        # A rebuild migration must change PRAGMA foreign_keys before its transaction
        # starts. Commit the prior migration's version stamp first.
        connection.commit()
        with connection:
            MIGRATIONS[version](connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
