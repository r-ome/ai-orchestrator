import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
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
"""


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
                    id, sandbox_id, proposal_id, mode, status, selected_service,
                    container_port, host_port, config_json, config_digest,
                    network_name, created_at, started_at, expires_at, last_activity_at
                ) VALUES (
                    :id, :sandbox_id, :proposal_id, :mode, :status, :selected_service,
                    :container_port, :host_port, :config_json, :config_digest,
                    :network_name, :created_at, :started_at, :expires_at, :last_activity_at
                )
                """,
                dict(values),
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
                payload={"mode": values["mode"], "host_port": values.get("host_port")},
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
