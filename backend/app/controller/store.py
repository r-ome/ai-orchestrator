import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from app.controller.config import ControllerSettings, get_controller_settings


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sandboxes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    project_name TEXT NOT NULL,
    volume_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    container_id TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_agent_per_sandbox
ON agent_runs(sandbox_id)
WHERE status IN ('created', 'running', 'replacing', 'stopping');

CREATE TABLE IF NOT EXISTS preview_runs (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    proposal_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'live',
    task_id TEXT,
    commit_sha TEXT,
    status TEXT NOT NULL,
    selected_service TEXT,
    container_port INTEGER NOT NULL,
    host_port INTEGER,
    config_json TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    network_name TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    stopped_at TEXT,
    expires_at TEXT,
    last_activity_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_preview_per_sandbox
ON preview_runs(sandbox_id)
WHERE status IN ('preparing', 'running', 'restarting', 'rebuilding', 'stopping');

CREATE TABLE IF NOT EXISTS assigned_ports (
    host_port INTEGER PRIMARY KEY,
    preview_run_id TEXT NOT NULL UNIQUE REFERENCES preview_runs(id),
    assigned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_rounds (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    proposal_digest TEXT NOT NULL,
    detected_mode TEXT NOT NULL,
    config_json TEXT NOT NULL,
    protected_files_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    review_round_id TEXT NOT NULL REFERENCES review_rounds(id),
    proposal_digest TEXT NOT NULL,
    config_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    approved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protected_file_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    path TEXT NOT NULL,
    content BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    approval_id INTEGER REFERENCES approvals(id)
);

CREATE INDEX IF NOT EXISTS protected_baseline_lookup
ON protected_file_baselines(sandbox_id, path, id DESC);

-- One row per sandbox that holds credentials on a shared database server.
-- owner_sandbox_id is the sandbox whose schema this row points at. A row whose
-- owner is itself owns the data; any other row is a guest and must never drop it.
CREATE TABLE IF NOT EXISTS shared_database_schemas (
    sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id),
    project_id TEXT NOT NULL,
    owner_sandbox_id TEXT NOT NULL,
    sharing TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    user_name TEXT NOT NULL,
    image TEXT NOT NULL,
    persistence TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS shared_database_by_project
ON shared_database_schemas(project_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id TEXT,
    run_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_secrets (
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    agent_run_id TEXT,
    branch TEXT NOT NULL,
    base_branch TEXT,
    base_commit TEXT NOT NULL,
    head_commit TEXT,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    -- Paths already dirty when the task branch was cut. A sandbox copied from
    -- a real repository arrives with untracked files the task never touches,
    -- and settlement must not read those as work the turn left uncommitted.
    baseline_dirty_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_sandbox
ON tasks(sandbox_id)
WHERE status IN ('open', 'reported', 'previewing', 'review');

CREATE TABLE IF NOT EXISTS planning_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    project_name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    turn_state TEXT NOT NULL DEFAULT 'idle',
    clarifier_provider TEXT NOT NULL,
    planner_provider TEXT NOT NULL,
    reviewer_provider TEXT NOT NULL,
    credential_profile TEXT NOT NULL DEFAULT 'default',
    max_review_turns INTEGER NOT NULL,
    review_turn INTEGER NOT NULL DEFAULT 0,
    plan_revision INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    understanding_summary TEXT NOT NULL DEFAULT '',
    feature_brief TEXT NOT NULL DEFAULT '',
    plan_spec_json TEXT,
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE INDEX IF NOT EXISTS planning_sessions_by_project
ON planning_sessions(project_id, created_at);

CREATE TABLE IF NOT EXISTS planning_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    raw_output TEXT NOT NULL DEFAULT '',
    revision INTEGER,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS planning_findings (
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    finding_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    planner_response TEXT NOT NULL DEFAULT '',
    raised_in_round INTEGER NOT NULL,
    last_seen_round INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, finding_id)
);

CREATE TABLE IF NOT EXISTS planning_plan_revisions (
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    plan_markdown TEXT NOT NULL,
    reviewer_approved INTEGER,
    reviewer_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    PRIMARY KEY (session_id, revision)
);

-- Code-level knowledge a delegated run needs so it does not have to
-- rediscover the architecture. Points at code; never contains it.
--
-- One row per session, no revisions. The context is derived from a plan the
-- human and the model already agreed on, so choosing between derivations is a
-- decision that should not exist. Regenerating resets this row. What keeps a
-- running delegation's context from changing underneath it is not a revision
-- number but `claim_context`, which refuses to regenerate once the session has
-- a delegation.
CREATE TABLE IF NOT EXISTS implementation_contexts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
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

-- `one_context_per_session` is created by migration 13, not here. The schema
-- script runs before migrations, so on a database that still holds revisions
-- the index would fail before the migration could collapse them.

-- One decomposition of a ready plan into work items, at a revision.
-- Revisions are added, never mutated: a completed run must keep pointing at
-- the definition it actually executed.
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    context_id TEXT REFERENCES implementation_contexts(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_delegation_revision_per_session
ON delegations(session_id, revision);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_delegation_per_sandbox
ON delegations(sandbox_id)
WHERE status IN ('ready', 'running', 'halted');

CREATE TABLE IF NOT EXISTS delegation_reviews (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_review_revision_per_delegation
ON delegation_reviews(delegation_id, revision);

CREATE UNIQUE INDEX IF NOT EXISTS one_generating_review_per_delegation
ON delegation_reviews(delegation_id)
WHERE status = 'generating';

-- Immutable once its delegation revision exists. There is no update path on
-- purpose: changing a definition means a new revision. Carries no provider or
-- model, because what the work is and how it is run are different questions.
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    scope TEXT NOT NULL,
    out_of_scope TEXT NOT NULL DEFAULT '',
    dependencies_json TEXT NOT NULL,
    files_json TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    write_scope_json TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    complexity TEXT NOT NULL,
    architecture_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_work_item_key_per_delegation
ON work_items(delegation_id, key);

-- One attempt at a work item. Appended, never overwritten, so a retry does
-- not erase what the first attempt cost.
CREATE TABLE IF NOT EXISTS work_item_runs (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id),
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    routing_source TEXT,
    task_id TEXT REFERENCES tasks(id),
    result_json TEXT,
    failure_kind TEXT,
    error TEXT,
    verification_json TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    duration_ms INTEGER,
    exit_code INTEGER,
    repair_count INTEGER NOT NULL DEFAULT 0,
    -- When the coding turn stopped running. A run stays 'running' after its
    -- turn finishes, because it settles only on a person's accept or reject.
    -- Without this stamp those two states are the same row, and startup
    -- reconciliation cannot tell a turn it must fail from a finished one it
    -- must leave alone. See `interrupted_turns`.
    turn_finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_number_per_work_item
ON work_item_runs(work_item_id, attempt);

-- Execution stays sequential. Eligibility is computed and shown in full; this
-- is what stops it being acted on concurrently.
CREATE UNIQUE INDEX IF NOT EXISTS one_running_run_per_delegation
ON work_item_runs(delegation_id)
WHERE status = 'running';

-- A person's routing choice for one work item. Separate from work_items
-- because a definition is immutable and an override is revisable.
CREATE TABLE IF NOT EXISTS work_item_routing (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    provider TEXT,
    model TEXT,
    actor TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


_CONTEXT_UPDATABLE_COLUMNS = frozenset(
    {
        "manifest_json",
        "commands_json",
        "inventory_json",
        "model",
        "error",
    }
)

_RUN_UPDATABLE_COLUMNS = frozenset(
    {
        "task_id",
        "model",
        "result_json",
        "failure_kind",
        "error",
        "verification_json",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "duration_ms",
        "exit_code",
        "repair_count",
        "routing_source",
    }
)


# Versions 1 to 8 are applied inline in `initialize` above, in the order this
# database grew. Anything added from here on goes through this table instead,
# so a new step is declared in one place rather than as another inline block.
#
# Each step must be safe to run against a database that SCHEMA already brought
# up to date, because SCHEMA describes the current shape and runs first. The
# helpers below check before they change anything.
FIRST_RUNNER_MIGRATION = 9


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _migration_9(connection: sqlite3.Connection) -> None:
    """Record which precedence rule chose a delegated run's model."""
    _add_column(connection, "work_item_runs", "routing_source", "TEXT")


def _migration_10(connection: sqlite3.Connection) -> None:
    """Retain feature-level integration review revisions."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS delegation_reviews (
            id TEXT PRIMARY KEY,
            delegation_id TEXT NOT NULL REFERENCES delegations(id),
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            settled_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_review_revision_per_delegation
        ON delegation_reviews(delegation_id, revision);
        CREATE UNIQUE INDEX IF NOT EXISTS one_generating_review_per_delegation
        ON delegation_reviews(delegation_id)
        WHERE status = 'generating';
        """
    )


def _migration_11(connection: sqlite3.Connection) -> None:
    """Remember what was already dirty when a task branch was cut."""
    _add_column(connection, "tasks", "baseline_dirty_json", "TEXT")


def _migration_12(connection: sqlite3.Connection) -> None:
    """Separate a run whose turn is in flight from one awaiting a decision."""
    _add_column(connection, "work_item_runs", "turn_finished_at", "TEXT")


def _migration_13(connection: sqlite3.Connection) -> None:
    """Collapse implementation contexts to one per session.

    The surviving row is the session's ready context if it has one, else its
    newest.

    A delegation may point at a revision that does not survive: `delegations`
    holds `context_id REFERENCES implementation_contexts(id)` and foreign keys
    are on, so deleting that revision fails the migration outright. Each such
    delegation is repointed at its session's surviving context first. That is
    the same plan's context, and under the collapsed model a session has one,
    so it is the row the delegation would resolve to anyway.
    """
    columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(implementation_contexts)"
        ).fetchall()
    }
    if "revision" not in columns:
        # A database created from the current schema. It has no revisions to
        # collapse, but the index below is still this migration's to create.
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_context_per_session
            ON implementation_contexts(session_id)
            """
        )
        return
    connection.execute(
        """
        CREATE TEMPORARY TABLE surviving_contexts AS
        SELECT id, session_id FROM (
            SELECT id, session_id, ROW_NUMBER() OVER (
                PARTITION BY session_id
                ORDER BY status = 'ready' DESC, revision DESC
            ) AS rank FROM implementation_contexts
        ) WHERE rank = 1
        """
    )
    # Before the delete, not after: the foreign key is checked per statement.
    connection.execute(
        """
        UPDATE delegations SET context_id = (
            SELECT s.id FROM surviving_contexts s
            WHERE s.session_id = delegations.session_id
        )
        WHERE context_id IS NOT NULL
          AND context_id NOT IN (SELECT id FROM surviving_contexts)
        """
    )
    connection.executescript(
        """
        DELETE FROM implementation_contexts
        WHERE id NOT IN (SELECT id FROM surviving_contexts);
        DROP TABLE surviving_contexts;
        DROP INDEX IF EXISTS one_context_revision_per_session;
        DROP INDEX IF EXISTS one_generating_context_per_session;
        ALTER TABLE implementation_contexts DROP COLUMN revision;
        CREATE UNIQUE INDEX IF NOT EXISTS one_context_per_session
        ON implementation_contexts(session_id);
        """
    )


MIGRATIONS: Mapping[int, Callable[[sqlite3.Connection], None]] = {
    9: _migration_9,
    10: _migration_10,
    11: _migration_11,
    12: _migration_12,
    13: _migration_13,
}


def _apply_migrations(connection: sqlite3.Connection) -> None:
    applied = {
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        MIGRATIONS[version](connection)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )


class ControllerStore:
    """Serialized SQLite access for controller-owned intent and audit state."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = RLock()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (_now(),),
            )
            try:
                connection.execute(
                    "ALTER TABLE sandboxes ADD COLUMN baseline_commit TEXT"
                )
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (3, ?)",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (4, ?)",
                (_now(),),
            )
            for statement in (
                "ALTER TABLE preview_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'live'",
                "ALTER TABLE preview_runs ADD COLUMN task_id TEXT",
                "ALTER TABLE preview_runs ADD COLUMN commit_sha TEXT",
            ):
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error):
                        raise
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (5, ?)",
                (_now(),),
            )
            # base_branch records the branch a task was cut from, so accept and
            # reject switch back to it instead of assuming 'main'. A sandbox
            # imported from a host repository keeps that repository's branch.
            try:
                connection.execute("ALTER TABLE tasks ADD COLUMN base_branch TEXT")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (6, ?)",
                (_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (7, ?)",
                (_now(),),
            )
            # model records which model produced a turn, at the time it ran.
            # The planning settings can change between a turn and the reading
            # of it, so the setting is not a record of what happened.
            try:
                connection.execute(
                    "ALTER TABLE planning_messages ADD COLUMN model TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (8, ?)",
                (_now(),),
            )
            _apply_migrations(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def register_sandbox(
        self,
        *,
        sandbox_id: str,
        project_id: str,
        project_name: str,
        source_path: str,
        volume_name: str,
        status: str,
        created_at: str,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            stale_owner = connection.execute(
                "SELECT id FROM sandboxes WHERE volume_name = ? AND id != ?",
                (volume_name, sandbox_id),
            ).fetchone()
            if stale_owner is not None:
                stale_sandbox_id = str(stale_owner["id"])
                retired_volume_name = (
                    f"{volume_name}#retired:{stale_sandbox_id}"
                )
                connection.execute(
                    """
                    UPDATE sandboxes
                    SET volume_name = ?, status = 'missing', updated_at = ?
                    WHERE id = ?
                    """,
                    (retired_volume_name, now, stale_sandbox_id),
                )
            connection.execute(
                """
                INSERT INTO projects(id, source_path, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET id = excluded.id
                """,
                (project_id, source_path, created_at or now),
            )
            connection.execute(
                """
                INSERT INTO sandboxes(
                    id, project_id, project_name, volume_name, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_name = excluded.project_name,
                    volume_name = excluded.volume_name,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    sandbox_id,
                    project_id,
                    project_name,
                    volume_name,
                    status,
                    created_at or now,
                    now,
                ),
            )

    def sandboxes(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sandboxes ORDER BY created_at"
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def update_sandbox_status(self, sandbox_id: str, status: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE sandboxes SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), sandbox_id),
            )

    def set_sandbox_baseline_commit(
        self,
        *,
        sandbox_id: str,
        baseline_commit: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE sandboxes SET baseline_commit = ?, updated_at = ? WHERE id = ?",
                (baseline_commit, _now(), sandbox_id),
            )

    def sandbox_baseline_commit(self, sandbox_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT baseline_commit FROM sandboxes WHERE id = ?",
                (sandbox_id,),
            ).fetchone()
        if row is None:
            return None
        return row["baseline_commit"]

    def sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandboxes WHERE id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def delete_sandbox(self, sandbox_id: str) -> None:
        """Deletes controller state after Docker resources leave the sandbox."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT project_id FROM sandboxes WHERE id = ?",
                (sandbox_id,),
            ).fetchone()
            if row is None:
                return
            project_key = str(row["project_id"])
            connection.execute(
                "DELETE FROM assigned_ports WHERE preview_run_id IN "
                "(SELECT id FROM preview_runs WHERE sandbox_id = ?)",
                (sandbox_id,),
            )
            connection.execute(
                "DELETE FROM protected_file_baselines WHERE sandbox_id = ?",
                (sandbox_id,),
            )
            connection.execute("DELETE FROM approvals WHERE sandbox_id = ?", (sandbox_id,))
            connection.execute(
                "DELETE FROM review_rounds WHERE sandbox_id = ?",
                (sandbox_id,),
            )
            connection.execute("DELETE FROM tasks WHERE sandbox_id = ?", (sandbox_id,))
            connection.execute("DELETE FROM events WHERE sandbox_id = ?", (sandbox_id,))
            connection.execute("DELETE FROM agent_runs WHERE sandbox_id = ?", (sandbox_id,))
            connection.execute(
                "DELETE FROM preview_runs WHERE sandbox_id = ?",
                (sandbox_id,),
            )
            connection.execute(
                "DELETE FROM shared_database_schemas WHERE sandbox_id = ?",
                (sandbox_id,),
            )
            connection.execute("DELETE FROM sandboxes WHERE id = ?", (sandbox_id,))
            remaining = connection.execute(
                "SELECT 1 FROM sandboxes WHERE project_id = ? LIMIT 1",
                (project_key,),
            ).fetchone()
            if remaining is None:
                connection.execute(
                    "DELETE FROM project_secrets WHERE project_id = ?",
                    (project_key,),
                )
                connection.execute("DELETE FROM projects WHERE id = ?", (project_key,))

    def create_task(
        self,
        *,
        task_id: str,
        sandbox_id: str,
        agent_run_id: str | None,
        branch: str,
        base_branch: str,
        base_commit: str,
        title: str,
        status: str,
    ) -> None:
        """Claims the sandbox's single open-task slot, or raises sqlite3.IntegrityError.

        The one_open_task_per_sandbox partial index is the only thing that
        decides the race, exactly as one_active_agent_per_sandbox does for
        coding agents. Callers must insert before touching git, so a losing
        caller has not yet changed the sandbox.
        """
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    id, sandbox_id, agent_run_id, branch, base_branch, base_commit,
                    status, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    sandbox_id,
                    agent_run_id,
                    branch,
                    base_branch,
                    base_commit,
                    status,
                    title,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                sandbox_id=sandbox_id,
                run_id=task_id,
                kind="task.started",
                payload={
                    "branch": branch,
                    "base_branch": base_branch,
                    "base_commit": base_commit,
                },
            )

    def set_task_base_branch(self, *, task_id: str, base_branch: str) -> None:
        """Records the branch the task was cut from, read back from git.

        The row has to exist before git runs, so the branch name can only be
        written once the branch script has reported it.
        """
        with self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET base_branch = ?, updated_at = ? WHERE id = ?",
                (base_branch, _now(), task_id),
            )

    def set_task_baseline_dirty(self, *, task_id: str, paths: Sequence[str]) -> None:
        """Records what git already called dirty before the turn ran.

        Written from the same script that cuts the branch, so the snapshot is
        of the worktree the turn is about to be handed.
        """
        with self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET baseline_dirty_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(sorted(set(paths))), _now(), task_id),
            )

    def delete_task(self, *, task_id: str) -> None:
        """Removes a task whose branch never got created. The events it wrote stay."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT sandbox_id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._event(
                connection,
                sandbox_id=str(row["sandbox_id"]),
                run_id=task_id,
                kind="task.discarded",
                payload={},
            )

    def advance_task_status(
        self,
        *,
        task_id: str,
        from_statuses: Iterable[str],
        to_status: str,
        head_commit: str | None = None,
        settled: bool = False,
    ) -> bool:
        """Moves a task only from one of from_statuses, in a single guarded UPDATE.

        The permitted source statuses travel in the WHERE clause, so a caller
        naming the wrong ones changes no row instead of forcing an illegal
        transition, and two concurrent callers cannot both win. Returns whether
        the row moved.
        """
        statuses = tuple(from_statuses)
        if not statuses:
            return False
        placeholders = ", ".join("?" for _ in statuses)
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks
                SET status = ?,
                    head_commit = COALESCE(?, head_commit),
                    updated_at = ?,
                    settled_at = CASE WHEN ? = 1 THEN ? ELSE settled_at END
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    to_status,
                    head_commit,
                    now,
                    1 if settled else 0,
                    now,
                    task_id,
                    *statuses,
                ),
            )
            if cursor.rowcount == 0:
                return False
            row = connection.execute(
                "SELECT sandbox_id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            self._event(
                connection,
                sandbox_id=str(row["sandbox_id"]) if row is not None else None,
                run_id=task_id,
                kind="task.status",
                payload={"status": to_status, "head_commit": head_commit},
            )
        return True

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return _row(row)

    def tasks_for_sandbox(self, sandbox_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE sandbox_id = ? ORDER BY created_at",
                (sandbox_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def open_task(
        self,
        sandbox_id: str,
        *,
        open_statuses: Iterable[str],
    ) -> dict[str, Any] | None:
        return self._active_run("tasks", sandbox_id, tuple(open_statuses))

    def create_planning_session(
        self,
        *,
        session_id: str,
        project_id: str,
        sandbox_id: str,
        project_name: str,
        title: str,
        status: str,
        clarifier_provider: str,
        planner_provider: str,
        reviewer_provider: str,
        credential_profile: str,
        max_review_turns: int,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO planning_sessions(
                    id, project_id, sandbox_id, project_name, title, status,
                    clarifier_provider, planner_provider, reviewer_provider,
                    credential_profile, max_review_turns, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    project_id,
                    sandbox_id,
                    project_name,
                    title,
                    status,
                    clarifier_provider,
                    planner_provider,
                    reviewer_provider,
                    credential_profile,
                    max_review_turns,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                sandbox_id=sandbox_id,
                run_id=session_id,
                kind="planning.started",
                payload={"status": status, "title": title},
            )

    def planning_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM planning_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _row(row)

    def planning_sessions_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM planning_sessions
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def start_implementation_context(self, values: Mapping[str, Any]) -> str | None:
        """Open the session's one context for generation. None if one is running.

        Regenerating resets the existing row rather than adding a revision, and
        keeps its id, so a `delegations.context_id` written earlier never points
        at a row that stopped existing. The read and the write share one
        serialized connection, so two racing callers cannot both claim it.
        """
        now = _now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT id, status FROM implementation_contexts WHERE session_id = ?",
                (values["session_id"],),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "generating":
                return None
            context_id = str(existing["id"]) if existing else str(values["id"])
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO implementation_contexts(
                        id, session_id, sandbox_id, status, provider,
                        model, created_at, updated_at
                    ) VALUES (
                        :id, :session_id, :sandbox_id, :status,
                        :provider, :model, :created_at, :updated_at
                    )
                    """,
                    {**dict(values), "created_at": now, "updated_at": now},
                )
            else:
                # Every settled column goes back to null. A regenerated context
                # that kept the previous manifest's commands would report a
                # result the current turn never produced.
                connection.execute(
                    """
                    UPDATE implementation_contexts SET
                        sandbox_id = :sandbox_id, status = :status,
                        provider = :provider, model = :model,
                        manifest_json = NULL, commands_json = NULL,
                        inventory_json = NULL, error = NULL, settled_at = NULL,
                        updated_at = :updated_at
                    WHERE id = :id
                    """,
                    {
                        **dict(values),
                        "id": context_id,
                        "updated_at": now,
                    },
                )
            self._event(
                connection,
                sandbox_id=str(values["sandbox_id"]),
                run_id=context_id,
                kind="context.generating",
                payload={"regenerated": existing is not None},
            )
        return context_id

    def implementation_context(self, context_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM implementation_contexts WHERE id = ?",
                (context_id,),
            ).fetchone()
        return _row(row)

    def implementation_context_for_session(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """The session's context, whatever its status. At most one exists."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM implementation_contexts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return _row(row)

    def settle_implementation_context(
        self,
        context_id: str,
        *,
        to_status: str,
        changes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Settle a context once, guarded against a late second writer."""
        now = _now()
        assignments = ["status = ?", "updated_at = ?", "settled_at = ?"]
        parameters: list[Any] = [to_status, now, now]
        for column, value in (changes or {}).items():
            if column not in _CONTEXT_UPDATABLE_COLUMNS:
                continue
            assignments.append(f"{column} = ?")
            parameters.append(value)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE implementation_contexts SET {", ".join(assignments)}
                WHERE id = ? AND status = 'generating'
                """,
                (*parameters, context_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM implementation_contexts WHERE id = ?",
                (context_id,),
            ).fetchone()
            updated = _row(row)
            self._event(
                connection,
                sandbox_id=str((updated or {}).get("sandbox_id") or ""),
                run_id=context_id,
                kind=f"context.{to_status}",
                payload={"revision": (updated or {}).get("revision")},
            )
        return updated

    def create_delegation_revision(
        self,
        delegation: Mapping[str, Any],
        items: Iterable[Mapping[str, Any]],
    ) -> None:
        """Write a delegation and all work items in one transaction."""
        now = _now()
        rows = list(items)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO delegations(
                    id, session_id, sandbox_id, context_id, revision, status,
                    created_at, updated_at
                ) VALUES (
                    :id, :session_id, :sandbox_id, :context_id, :revision,
                    :status, :created_at, :updated_at
                )
                """,
                {**dict(delegation), "created_at": now, "updated_at": now},
            )
            connection.executemany(
                """
                INSERT INTO work_items(
                    id, delegation_id, key, position, title, objective, scope,
                    out_of_scope, dependencies_json, files_json, symbols_json,
                    write_scope_json, acceptance_criteria_json, verification_json,
                    complexity, architecture_json, risks_json, created_at
                ) VALUES (
                    :id, :delegation_id, :key, :position, :title, :objective,
                    :scope, :out_of_scope, :dependencies_json, :files_json,
                    :symbols_json, :write_scope_json, :acceptance_criteria_json,
                    :verification_json, :complexity, :architecture_json,
                    :risks_json, :created_at
                )
                """,
                [{**dict(row), "created_at": now} for row in rows],
            )
            self._event(
                connection,
                sandbox_id=str(delegation["sandbox_id"]),
                run_id=str(delegation["id"]),
                kind="delegation.created",
                payload={
                    "revision": delegation["revision"],
                    "work_items": len(rows),
                },
            )

    def delegation(self, delegation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
        return _row(row)

    def delegations_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegations
                WHERE session_id = ? ORDER BY revision DESC
                """,
                (session_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def delegations_for_sandbox(self, sandbox_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegations
                WHERE sandbox_id = ? ORDER BY created_at DESC
                """,
                (sandbox_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def next_delegation_revision(self, session_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS highest
                FROM delegations WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return int(row["highest"]) + 1

    def create_delegation_review(self, values: Mapping[str, Any]) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO delegation_reviews(
                    id, delegation_id, revision, status, provider, model,
                    created_at, updated_at
                ) VALUES (
                    :id, :delegation_id, :revision, :status, :provider, :model,
                    :created_at, :updated_at
                )
                """,
                {**dict(values), "created_at": now, "updated_at": now},
            )
            self._event(
                connection,
                sandbox_id=None,
                run_id=str(values["id"]),
                kind="delegation_review.generating",
                payload={"revision": values["revision"]},
            )

    def settle_delegation_review(
        self,
        review_id: str,
        *,
        to_status: str,
        result_json: str | None = None,
        model: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_reviews
                SET status = ?, result_json = ?, model = ?, error = ?,
                    updated_at = ?, settled_at = ?
                WHERE id = ? AND status = 'generating'
                """,
                (to_status, result_json, model, error, now, now, review_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM delegation_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
            self._event(
                connection,
                sandbox_id=None,
                run_id=review_id,
                kind=f"delegation_review.{to_status}",
                payload={"status": to_status},
            )
        return _row(row)

    def delegation_reviews(self, delegation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegation_reviews
                WHERE delegation_id = ? ORDER BY revision DESC
                """,
                (delegation_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def delegation_review(self, review_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        return _row(row)

    def next_delegation_review_revision(self, delegation_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS highest
                FROM delegation_reviews WHERE delegation_id = ?
                """,
                (delegation_id,),
            ).fetchone()
        return int(row["highest"]) + 1

    def transition_delegation(
        self,
        delegation_id: str,
        *,
        to_status: str,
        from_statuses: Iterable[str],
        terminal: bool = False,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        allowed = tuple(from_statuses)
        if not allowed:
            return None
        placeholders = ", ".join("?" for _ in allowed)
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE delegations
                SET status = ?, updated_at = ?, settled_at = ?, error = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    to_status,
                    now,
                    now if terminal else None,
                    error,
                    delegation_id,
                    *allowed,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
            updated = _row(row)
            self._event(
                connection,
                sandbox_id=str((updated or {}).get("sandbox_id") or ""),
                run_id=delegation_id,
                kind=f"delegation.{to_status}",
                payload={"status": to_status},
            )
        return updated

    def work_items(self, delegation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM work_items
                WHERE delegation_id = ? ORDER BY position
                """,
                (delegation_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def work_item(self, work_item_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE id = ?",
                (work_item_id,),
            ).fetchone()
        return _row(row)

    def start_work_item_run(self, values: Mapping[str, Any]) -> None:
        """Append one attempt and claim the delegation's running slot."""
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO work_item_runs(
                    id, work_item_id, delegation_id, attempt, status, provider,
                    model, task_id, created_at, updated_at
                ) VALUES (
                    :id, :work_item_id, :delegation_id, :attempt, :status,
                    :provider, :model, :task_id, :created_at, :updated_at
                )
                """,
                {**dict(values), "created_at": now, "updated_at": now},
            )
            self._event(
                connection,
                sandbox_id=None,
                run_id=str(values["id"]),
                kind="work_item_run.running",
                payload={
                    "work_item_id": values["work_item_id"],
                    "attempt": values["attempt"],
                },
            )

    def settle_work_item_run(
        self,
        run_id: str,
        *,
        to_status: str,
        changes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        assignments = ["status = ?", "updated_at = ?", "settled_at = ?"]
        parameters: list[Any] = [to_status, now, now]
        for column, value in (changes or {}).items():
            if column not in _RUN_UPDATABLE_COLUMNS:
                continue
            assignments.append(f"{column} = ?")
            parameters.append(value)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE work_item_runs SET {", ".join(assignments)}
                WHERE id = ? AND status = 'running'
                """,
                (*parameters, run_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM work_item_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            self._event(
                connection,
                sandbox_id=None,
                run_id=run_id,
                kind=f"work_item_run.{to_status}",
                payload={"status": to_status},
            )
        return _row(row)

    def work_item_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM work_item_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _row(row)

    def finish_work_item_turn(self, run_id: str) -> None:
        """Stamp that the coding turn stopped, leaving the run for a person.

        Idempotent, and only ever moves a run that is still 'running': a run
        already settled by an accept or a reject keeps its own record.
        """
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE work_item_runs
                SET turn_finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND turn_finished_at IS NULL
                """,
                (_now(), _now(), run_id),
            )

    def record_work_item_run(
        self,
        run_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        values = {
            column: value
            for column, value in changes.items()
            if column in _RUN_UPDATABLE_COLUMNS
        }
        if not values:
            return None
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self._connection() as connection:
            connection.execute(
                f"""
                UPDATE work_item_runs
                SET {assignments}, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (*values.values(), _now(), run_id),
            )
            row = connection.execute(
                "SELECT * FROM work_item_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _row(row)

    def work_item_runs(self, delegation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM work_item_runs
                WHERE delegation_id = ? ORDER BY work_item_id, attempt
                """,
                (delegation_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def next_attempt_number(self, work_item_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) AS highest
                FROM work_item_runs WHERE work_item_id = ?
                """,
                (work_item_id,),
            ).fetchone()
        return int(row["highest"]) + 1

    def set_work_item_routing(
        self,
        work_item_id: str,
        *,
        provider: str | None,
        model: str | None,
        actor: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO work_item_routing(
                    work_item_id, provider, model, actor, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    actor = excluded.actor,
                    updated_at = excluded.updated_at
                """,
                (work_item_id, provider, model, actor, _now()),
            )

    def clear_work_item_routing(self, work_item_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM work_item_routing WHERE work_item_id = ?",
                (work_item_id,),
            )

    def work_item_routing(self, delegation_id: str) -> dict[str, dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT routing.* FROM work_item_routing AS routing
                JOIN work_items AS item ON item.id = routing.work_item_id
                WHERE item.delegation_id = ?
                """,
                (delegation_id,),
            ).fetchall()
        return {str(row["work_item_id"]): dict(row) for row in rows}

    def advance_planning_status(
        self,
        *,
        session_id: str,
        from_statuses: Iterable[str],
        to_status: str,
        settled: bool = False,
        failure_reason: str | None = None,
    ) -> bool:
        """Moves a session only from one of from_statuses, in a guarded UPDATE."""
        statuses = tuple(from_statuses)
        if not statuses:
            return False
        placeholders = ", ".join("?" for _ in statuses)
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE planning_sessions
                SET status = ?,
                    updated_at = ?,
                    settled_at = CASE WHEN ? = 1 THEN ? ELSE settled_at END,
                    failure_reason = COALESCE(?, failure_reason)
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    to_status,
                    now,
                    1 if settled else 0,
                    now,
                    failure_reason,
                    session_id,
                    *statuses,
                ),
            )
            if cursor.rowcount == 0:
                return False
            row = connection.execute(
                "SELECT sandbox_id FROM planning_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            self._event(
                connection,
                sandbox_id=str(row["sandbox_id"]) if row is not None else None,
                run_id=session_id,
                kind="planning.status",
                payload={"status": to_status},
            )
        return True

    def claim_planning_turn(self, session_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE planning_sessions
                SET turn_state = 'running', updated_at = ?
                WHERE id = ? AND turn_state = 'idle'
                """,
                (_now(), session_id),
            )
            return cursor.rowcount == 1

    def release_planning_turn(self, session_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE planning_sessions
                SET turn_state = 'idle', updated_at = ?
                WHERE id = ?
                """,
                (_now(), session_id),
            )

    def running_planning_sessions(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM planning_sessions
                WHERE turn_state = 'running'
                ORDER BY created_at
                """
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def interrupted_turns(self) -> dict[str, list[dict[str, Any]]]:
        """Rows whose turn was still in flight, keyed by table.

        A turn now runs on a background thread (see `app.jobs`), and a daemon
        thread dies with the process. Nothing then settles the row it claimed,
        so a generating context would block its session's unique index forever
        and a running work item would block its delegation. Startup
        reconciliation reads this and fails each one.

        A work item run whose turn already finished is excluded. It is still
        'running' on purpose — it holds a verified commit and waits for a
        person to accept or reject it — so failing it would throw that work
        away. `turn_finished_at` is what tells the two apart.
        """
        queries = {
            "implementation_contexts": (
                "SELECT * FROM implementation_contexts WHERE status = 'generating'"
            ),
            "delegation_reviews": (
                "SELECT * FROM delegation_reviews WHERE status = 'generating'"
            ),
            "work_item_runs": (
                "SELECT * FROM work_item_runs "
                "WHERE status = 'running' AND turn_finished_at IS NULL"
            ),
        }
        found: dict[str, list[dict[str, Any]]] = {}
        with self._connection() as connection:
            for table, query in queries.items():
                rows = connection.execute(query).fetchall()
                found[table] = [_row(row) for row in rows if row is not None]
        return found

    def append_planning_message(
        self,
        *,
        session_id: str,
        role: str,
        text: str,
        payload: Mapping[str, Any] | None = None,
        raw_output: str = "",
        revision: int | None = None,
        model: str = "",
    ) -> int:
        with self._connection() as connection:
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM planning_messages
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO planning_messages(
                    session_id, sequence, role, text, payload_json, raw_output,
                    revision, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    role,
                    text,
                    _json(payload or {}),
                    raw_output,
                    revision,
                    model,
                    _now(),
                ),
            )
        return sequence

    def planning_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM planning_messages
                WHERE session_id = ?
                ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def planning_message(self, session_id: str, sequence: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM planning_messages
                WHERE session_id = ? AND sequence = ?
                """,
                (session_id, sequence),
            ).fetchone()
        return _row(row) if row is not None else None

    def set_planning_understanding(self, *, session_id: str, summary: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE planning_sessions
                SET understanding_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (summary, _now(), session_id),
            )

    def freeze_planning_brief(
        self,
        *,
        session_id: str,
        brief: str,
        confirmed: bool,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE planning_sessions
                SET feature_brief = ?, confirmed = ?, updated_at = ?
                WHERE id = ?
                """,
                (brief, 1 if confirmed else 0, _now(), session_id),
            )

    def record_plan_revision(
        self,
        *,
        session_id: str,
        revision: int,
        plan_json: Mapping[str, Any],
        plan_markdown: str,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO planning_plan_revisions(
                    session_id, revision, plan_json, plan_markdown, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, revision, _json(plan_json), plan_markdown, now),
            )
            connection.execute(
                """
                UPDATE planning_sessions
                SET plan_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (revision, now, session_id),
            )

    def record_review_result(
        self,
        *,
        session_id: str,
        revision: int,
        approved: bool,
        summary: str,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE planning_plan_revisions
                SET reviewer_approved = ?, reviewer_summary = ?, reviewed_at = ?
                WHERE session_id = ? AND revision = ?
                """,
                (1 if approved else 0, summary, now, session_id, revision),
            )
            connection.execute(
                """
                UPDATE planning_sessions
                SET review_turn = review_turn + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )

    def plan_revisions(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM planning_plan_revisions
                WHERE session_id = ?
                ORDER BY revision
                """,
                (session_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def upsert_planning_finding(
        self,
        *,
        session_id: str,
        finding_id: str,
        severity: str,
        text: str,
        status: str,
        round_number: int,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO planning_findings(
                    session_id, finding_id, severity, text, status,
                    raised_in_round, last_seen_round, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, finding_id) DO UPDATE SET
                    severity = excluded.severity,
                    text = excluded.text,
                    status = excluded.status,
                    last_seen_round = excluded.last_seen_round,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    finding_id,
                    severity,
                    text,
                    status,
                    round_number,
                    round_number,
                    _now(),
                ),
            )

    def set_finding_response(
        self,
        *,
        session_id: str,
        finding_id: str,
        status: str,
        planner_response: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE planning_findings
                SET status = ?, planner_response = ?, updated_at = ?
                WHERE session_id = ? AND finding_id = ?
                """,
                (status, planner_response, _now(), session_id, finding_id),
            )

    def resolve_unseen_findings(self, *, session_id: str, round_number: int) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE planning_findings
                SET status = 'resolved', updated_at = ?
                WHERE session_id = ?
                    AND status != 'resolved'
                    AND last_seen_round < ?
                """,
                (_now(), session_id, round_number),
            )
            return cursor.rowcount

    def planning_findings(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM planning_findings
                WHERE session_id = ?
                ORDER BY raised_in_round, finding_id
                """,
                (session_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def set_plan_spec(self, *, session_id: str, plan_spec: Mapping[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE planning_sessions
                SET plan_spec_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json(plan_spec), _now(), session_id),
            )

    def record_initial_baseline(
        self,
        sandbox_id: str,
        files: Mapping[str, bytes],
        hashes: Mapping[str, str],
    ) -> None:
        recorded_at = _now()
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM protected_file_baselines
                WHERE sandbox_id = ? AND source = 'original'
                LIMIT 1
                """,
                (sandbox_id,),
            ).fetchone()
            if existing:
                return
            connection.executemany(
                """
                INSERT INTO protected_file_baselines(
                    sandbox_id, path, content, content_hash, source, recorded_at
                ) VALUES (?, ?, ?, ?, 'original', ?)
                """,
                [
                    (sandbox_id, path, content, hashes[path], recorded_at)
                    for path, content in sorted(files.items())
                    if path in hashes
                ],
            )

    def latest_baseline(self, sandbox_id: str) -> dict[str, tuple[bytes, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT path, content, content_hash
                FROM protected_file_baselines AS baseline
                WHERE sandbox_id = ?
                  AND id = (
                    SELECT MAX(id) FROM protected_file_baselines AS latest
                    WHERE latest.sandbox_id = baseline.sandbox_id
                      AND latest.path = baseline.path
                  )
                """,
                (sandbox_id,),
            ).fetchall()
        return {
            row["path"]: (bytes(row["content"]), row["content_hash"])
            for row in rows
        }

    def create_review(
        self,
        *,
        review_id: str,
        sandbox_id: str,
        proposal_digest: str,
        detected_mode: str,
        config: Mapping[str, Any],
        protected_files: Mapping[str, str],
        changes: Iterable[Mapping[str, Any]],
        created_at: str,
        expires_at: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO review_rounds(
                    id, sandbox_id, proposal_digest, detected_mode, config_json,
                    protected_files_json, changes_json, created_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    review_id,
                    sandbox_id,
                    proposal_digest,
                    detected_mode,
                    _json(config),
                    _json(protected_files),
                    _json(list(changes)),
                    created_at,
                    expires_at,
                ),
            )
            self._event(
                connection,
                sandbox_id=sandbox_id,
                run_id=review_id,
                kind="preview.proposed",
                payload={"digest": proposal_digest, "mode": detected_mode},
            )

    def review(self, review_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_rounds WHERE id = ?",
                (review_id,),
            ).fetchone()
        return _row(row)

    def latest_approval(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM approvals
                WHERE sandbox_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def approve_review(
        self,
        *,
        review_id: str,
        sandbox_id: str,
        proposal_digest: str,
        config: Mapping[str, Any],
        actor: str,
        files: Mapping[str, bytes],
        hashes: Mapping[str, str],
    ) -> int:
        approved_at = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO approvals(
                    sandbox_id, review_round_id, proposal_digest,
                    config_json, actor, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sandbox_id,
                    review_id,
                    proposal_digest,
                    _json(config),
                    actor,
                    approved_at,
                ),
            )
            approval_id = int(cursor.lastrowid)
            previous_paths = {
                str(row["path"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT path FROM protected_file_baselines
                    WHERE sandbox_id = ?
                    """,
                    (sandbox_id,),
                ).fetchall()
            }
            approved_paths = sorted(set(hashes) | previous_paths)
            connection.executemany(
                """
                INSERT INTO protected_file_baselines(
                    sandbox_id, path, content, content_hash, source,
                    recorded_at, approval_id
                ) VALUES (?, ?, ?, ?, 'approved', ?, ?)
                """,
                [
                    (
                        sandbox_id,
                        path,
                        files.get(path, b""),
                        hashes.get(path, ""),
                        approved_at,
                        approval_id,
                    )
                    for path in approved_paths
                ],
            )
            connection.execute(
                "UPDATE review_rounds SET status = 'approved' WHERE id = ?",
                (review_id,),
            )
            self._event(
                connection,
                sandbox_id=sandbox_id,
                run_id=review_id,
                kind="preview.approved",
                payload={"approval_id": approval_id, "actor": actor},
            )
        return approval_id

    def active_agent(self, sandbox_id: str) -> dict[str, Any] | None:
        return self._active_run(
            "agent_runs",
            sandbox_id,
            ("created", "running", "replacing", "stopping"),
        )

    def active_agents(self) -> list[dict[str, Any]]:
        return self._active_runs(
            "agent_runs",
            ("created", "running", "replacing", "stopping"),
        )

    def start_agent_run(
        self,
        *,
        run_id: str,
        sandbox_id: str,
        provider: str,
        container_id: str | None = None,
        status: str = "created",
    ) -> None:
        created_at = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, sandbox_id, container_id, provider, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, sandbox_id, container_id, provider, status, created_at),
            )
            self._event(
                connection,
                sandbox_id=sandbox_id,
                run_id=run_id,
                kind="agent.created",
                payload={"provider": provider},
            )

    def update_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        container_id: str | None = None,
    ) -> None:
        finished_at = _now() if status in {"stopped", "failed", "missing"} else None
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, container_id = COALESCE(?, container_id),
                    finished_at = COALESCE(?, finished_at)
                WHERE id = ?
                """,
                (status, container_id, finished_at, run_id),
            )

    def active_preview(self, sandbox_id: str) -> dict[str, Any] | None:
        return self._active_run(
            "preview_runs",
            sandbox_id,
            ("preparing", "running", "restarting", "rebuilding", "stopping"),
        )

    def active_previews(self) -> list[dict[str, Any]]:
        return self._active_runs(
            "preview_runs",
            ("preparing", "running", "restarting", "rebuilding", "stopping"),
        )

    def create_preview_run(self, values: Mapping[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO preview_runs(
                    id, sandbox_id, proposal_id, mode, kind, task_id, commit_sha,
                    status, selected_service,
                    container_port, host_port, config_json, config_digest,
                    network_name, created_at, started_at, expires_at, last_activity_at
                ) VALUES (
                    :id, :sandbox_id, :proposal_id, :mode, :kind, :task_id, :commit_sha,
                    :status, :selected_service,
                    :container_port, :host_port, :config_json, :config_digest,
                    :network_name, :created_at, :started_at, :expires_at, :last_activity_at
                )
                """,
                {"kind": "live", "task_id": None, "commit_sha": None, **dict(values)},
            )
            if values.get("host_port"):
                connection.execute(
                    """
                    INSERT INTO assigned_ports(host_port, preview_run_id, assigned_at)
                    VALUES (?, ?, ?)
                    """,
                    (values["host_port"], values["id"], _now()),
                )
            self._event(
                connection,
                sandbox_id=str(values["sandbox_id"]),
                run_id=str(values["id"]),
                kind="preview.started",
                payload={
                    "mode": values["mode"],
                    "kind": values.get("kind", "live"),
                    "task_id": values.get("task_id"),
                    "commit_sha": values.get("commit_sha"),
                    "host_port": values.get("host_port"),
                },
            )

    def update_preview_run(self, run_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "host_port",
            "network_name",
            "started_at",
            "stopped_at",
            "expires_at",
            "last_activity_at",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE preview_runs SET {assignments} WHERE id = ?",
                (*values.values(), run_id),
            )
            if values.get("status") in {"stopped", "expired", "failed", "missing"}:
                connection.execute(
                    "DELETE FROM assigned_ports WHERE preview_run_id = ?",
                    (run_id,),
                )

    def preview_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM preview_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _row(row)

    def expired_previews(self, now: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM preview_runs
                WHERE status = 'running' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def touch_preview(self, run_id: str, *, expires_at: str | None) -> None:
        now = _now()
        self.update_preview_run(
            run_id,
            last_activity_at=now,
            expires_at=expires_at,
        )

    def record_shared_schema(
        self,
        *,
        sandbox_id: str,
        project_id: str,
        owner_sandbox_id: str,
        sharing: str,
        schema_name: str,
        user_name: str,
        image: str,
        persistence: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO shared_database_schemas(
                    sandbox_id, project_id, owner_sandbox_id, sharing,
                    schema_name, user_name, image, persistence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sandbox_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    owner_sandbox_id = excluded.owner_sandbox_id,
                    sharing = excluded.sharing,
                    schema_name = excluded.schema_name,
                    user_name = excluded.user_name,
                    image = excluded.image,
                    persistence = excluded.persistence
                """,
                (
                    sandbox_id,
                    project_id,
                    owner_sandbox_id,
                    sharing,
                    schema_name,
                    user_name,
                    image,
                    persistence,
                    _now(),
                ),
            )

    def shared_schema(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM shared_database_schemas WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def shared_schemas_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shared_database_schemas
                WHERE project_id = ?
                ORDER BY created_at
                """,
                (project_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def delete_shared_schema(self, sandbox_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM shared_database_schemas WHERE sandbox_id = ?",
                (sandbox_id,),
            )

    def event(
        self,
        *,
        sandbox_id: str | None,
        run_id: str | None,
        kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self._connection() as connection:
            self._event(
                connection,
                sandbox_id=sandbox_id,
                run_id=run_id,
                kind=kind,
                payload=payload,
            )

    def events_for_run(
        self,
        run_id: str,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, sandbox_id, run_id, kind, payload_json, created_at
            FROM events
            WHERE run_id = ?
        """
        parameters: list[Any] = [run_id]
        if kind is not None:
            query += " AND kind = ?"
            parameters.append(kind)
        query += " ORDER BY id"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            try:
                event["payload"] = json.loads(str(event.pop("payload_json")))
            except (TypeError, ValueError):
                event["payload"] = {}
            events.append(event)
        return events

    def set_project_secrets(self, project_id: str, values: Mapping[str, str]) -> None:
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                """
                INSERT INTO project_secrets(project_id, name, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [(project_id, name, value, now) for name, value in values.items()],
            )

    def delete_project_secret(self, project_id: str, name: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM project_secrets WHERE project_id = ? AND name = ?",
                (project_id, name),
            )

    def project_secret_names(self, project_id: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name FROM project_secrets WHERE project_id = ? ORDER BY name",
                (project_id,),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def project_secret_entries(self, project_id: str) -> list[dict[str, str]]:
        """Names and update timestamps only, for API responses that must not leak values."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT name, updated_at FROM project_secrets
                WHERE project_id = ? ORDER BY name
                """,
                (project_id,),
            ).fetchall()
        return [{"name": str(row["name"]), "updated_at": str(row["updated_at"])} for row in rows]

    def project_secrets(self, project_id: str) -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name, value FROM project_secrets WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {str(row["name"]): str(row["value"]) for row in rows}

    def _active_run(
        self,
        table: str,
        sandbox_id: str,
        statuses: tuple[str, ...],
    ) -> dict[str, Any] | None:
        placeholders = ", ".join("?" for _ in statuses)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM {table}
                WHERE sandbox_id = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (sandbox_id, *statuses),
            ).fetchone()
        return _row(row)

    def _active_runs(
        self,
        table: str,
        statuses: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in statuses)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM {table}
                WHERE status IN ({placeholders})
                ORDER BY created_at
                """,
                statuses,
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        sandbox_id: str | None,
        run_id: str | None,
        kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO events(sandbox_id, run_id, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sandbox_id, run_id, kind, _json(payload), _now()),
        )


_stores: dict[Path, ControllerStore] = {}
_stores_lock = RLock()


def get_controller_store() -> ControllerStore:
    return controller_store_for_settings(get_controller_settings())


def controller_store_for_settings(settings: ControllerSettings) -> ControllerStore:
    path = settings.database_path
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = ControllerStore(path)
            store.initialize()
            _stores[path] = store
        return store


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
