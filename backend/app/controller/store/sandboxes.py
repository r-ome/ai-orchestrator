import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from app.controller.store.lifecycle_status import SandboxLifecycleStatus, source_statuses

from ._shared import _ensure_immutable_manifest_value, _now, _row
from .errors import (
    SandboxAdmissionError,
    SandboxLeaseBlockedByWriterError,
    SandboxLeaseHeldError,
)


class SandboxesMixin:
    """Owns sandbox state and lifecycle leases."""

    def register_v1_sandbox(
        self,
        *,
        sandbox_id: str,
        project_id: str,
        project_name: str,
        volume_name: str,
        created_at: str,
    ) -> tuple[dict[str, Any], bool]:
        """Record v1 sandbox intent without creating a Docker resource.

        Managed sandboxes are keyed by a remote project.
        """
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sandboxes(
                    id, project_id, project_name, volume_name, status,
                    lifecycle_version, desired_state, lifecycle_status,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, 'creating',
                    'v1', 'active', ?, ?, ?
                )
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    sandbox_id,
                    project_id,
                    project_name,
                    volume_name,
                    SandboxLifecycleStatus.CREATING.value,
                    created_at or now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("v1 sandbox registration did not persist a sandbox")
        return result, cursor.rowcount == 1

    def update_sandbox_manifest(
        self,
        *,
        sandbox_id: str,
        values: Mapping[str, Any],
        from_lifecycle_statuses: Iterable[str] | None = None,
        allow_unset_lifecycle_status: bool = False,
    ) -> bool:
        """Write manifest columns with an optional atomic lifecycle guard."""
        fields = (
            "lifecycle_version",
            "feature_key",
            "feature_title",
            "desired_state",
            "lifecycle_status",
            "last_error",
            "base_ref",
            "created_base_commit",
            "current_base_commit",
            "pending_base_commit",
            "feature_branch",
            "agent_provider",
            "network_policy",
            "db_engine",
            "db_name",
            "schema_baseline_hash",
            "db_data_volume",
            "publish_remote",
            "remote_branch",
            "pr_requested",
        )
        unknown_fields = set(values).difference(fields)
        if unknown_fields:
            raise ValueError(f"unknown sandbox manifest fields: {sorted(unknown_fields)!r}")
        missing_fields = set(fields).difference(values)
        if missing_fields:
            raise ValueError(f"missing sandbox manifest fields: {sorted(missing_fields)!r}")

        with self._connection() as connection:
            current = connection.execute(
                """
                SELECT feature_key, created_base_commit FROM sandboxes WHERE id = ?
                """,
                (sandbox_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"sandbox {sandbox_id!r} is not registered")
            _ensure_immutable_manifest_value(
                field="feature_key",
                existing=current["feature_key"],
                requested=values["feature_key"],
            )
            _ensure_immutable_manifest_value(
                field="created_base_commit",
                existing=current["created_base_commit"],
                requested=values["created_base_commit"],
            )

            assignments = ", ".join(f"{field} = ?" for field in fields)
            parameters = [
                int(bool(values[field])) if field == "pr_requested" else values[field]
                for field in fields
            ]
            lifecycle_guard = ""
            lifecycle_parameters: tuple[str, ...] = ()
            if from_lifecycle_statuses is not None:
                lifecycle_parameters = tuple(from_lifecycle_statuses)
                allowed = []
                if lifecycle_parameters:
                    placeholders = ", ".join("?" for _ in lifecycle_parameters)
                    allowed.append(f"lifecycle_status IN ({placeholders})")
                if allow_unset_lifecycle_status:
                    allowed.append("lifecycle_status IS NULL")
                if not allowed:
                    return False
                lifecycle_guard = f" AND ({' OR '.join(allowed)})"
            cursor = connection.execute(
                f"""
                UPDATE sandboxes
                SET {assignments}, updated_at = ?
                WHERE id = ?{lifecycle_guard}
                """,
                (*parameters, _now(), sandbox_id, *lifecycle_parameters),
            )
        return cursor.rowcount == 1

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

    def set_sandbox_dirty_baseline_if_missing(
        self,
        *,
        sandbox_id: str,
        baseline_json: str,
    ) -> bool:
        """Record one immutable dirty baseline for a sandbox.

        The conditional update prevents a later task or review from replacing
        the original snapshot with its current worktree state.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandboxes
                SET dirty_baseline_json = ?, updated_at = ?
                WHERE id = ? AND dirty_baseline_json IS NULL
                """,
                (baseline_json, _now(), sandbox_id),
            )
        return cursor.rowcount == 1

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

    def sandbox_publication(self, sandbox_id: str) -> dict[str, Any] | None:
        """Return observed remote Git and PR state for one managed sandbox."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_publications WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def record_sandbox_publication(
        self,
        *,
        sandbox_id: str,
        remote_branch: str,
        last_pushed_commit: str | None,
        remote_branch_sha: str | None,
        last_error: str | None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        pr_state: str | None = None,
        pr_merged_at: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Upsert observed Git and PR state without asserting publication intent.

        `session_id` names the planning session the publication belongs to. It
        is COALESCEd like the PR columns, so the call that observes a later
        fact — a push failure, a refreshed PR state — keeps the attribution the
        publishing call established rather than clearing it.
        """
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sandbox_publications(
                    sandbox_id, remote_branch, last_pushed_commit, remote_branch_sha,
                    pr_number, pr_url, pr_state, pr_merged_at, last_error, updated_at,
                    session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sandbox_id) DO UPDATE SET
                    session_id = COALESCE(
                        excluded.session_id, sandbox_publications.session_id
                    ),
                    remote_branch = excluded.remote_branch,
                    last_pushed_commit = excluded.last_pushed_commit,
                    remote_branch_sha = excluded.remote_branch_sha,
                    pr_number = COALESCE(excluded.pr_number, sandbox_publications.pr_number),
                    pr_url = COALESCE(excluded.pr_url, sandbox_publications.pr_url),
                    pr_state = COALESCE(excluded.pr_state, sandbox_publications.pr_state),
                    pr_merged_at = COALESCE(
                        excluded.pr_merged_at, sandbox_publications.pr_merged_at
                    ),
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    sandbox_id,
                    remote_branch,
                    last_pushed_commit,
                    remote_branch_sha,
                    pr_number,
                    pr_url,
                    pr_state,
                    pr_merged_at,
                    last_error,
                    now,
                    session_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_publications WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("sandbox publication did not persist")
        return result

    def publication_owner_session(self, sandbox_id: str) -> str | None:
        """The planning session whose work a push from this sandbox carries.

        A sandbox has one feature branch, and every delegation in it merges
        into that branch, so the session that most recently settled a
        delegation is the one whose work is being pushed. Returns None when
        the sandbox has no delegation, because then nothing built the branch
        and no session should be credited with the pull request.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT delegation.session_id AS session_id
                FROM delegations AS delegation
                JOIN planning_sessions AS session
                    ON session.id = delegation.session_id
                WHERE session.sandbox_id = ?
                ORDER BY COALESCE(delegation.settled_at, delegation.updated_at) DESC
                LIMIT 1
                """,
                (sandbox_id,),
            ).fetchone()
        return str(row["session_id"]) if row is not None else None

    def sandbox_engine_detection(self, sandbox_id: str) -> dict[str, Any] | None:
        """Return one sandbox's persisted detection and command snapshot."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_engine_detections WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def record_sandbox_engine_detection(
        self,
        *,
        sandbox_id: str,
        signals: Sequence[Mapping[str, Any]],
        proposed_engine: str | None,
        migrate_commands: Sequence[str],
        seed_commands: Sequence[str],
        commands_source: Mapping[str, str],
        detected_at_commit: str,
    ) -> dict[str, Any]:
        """Persist a proposal from a controller-read, pinned project state."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sandbox_engine_detections(
                    sandbox_id, signals_json, proposed_engine, confirmed_engine,
                    migrate_commands_json, seed_commands_json, commands_source,
                    detected_at_commit, actor, confirmed_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(sandbox_id) DO UPDATE SET
                    signals_json = excluded.signals_json,
                    proposed_engine = excluded.proposed_engine,
                    migrate_commands_json = excluded.migrate_commands_json,
                    seed_commands_json = excluded.seed_commands_json,
                    commands_source = excluded.commands_source,
                    detected_at_commit = excluded.detected_at_commit
                WHERE sandbox_engine_detections.confirmed_engine IS NULL
                """,
                (
                    sandbox_id,
                    json.dumps(list(signals), sort_keys=True),
                    proposed_engine,
                    json.dumps(list(migrate_commands)),
                    json.dumps(list(seed_commands)),
                    json.dumps(dict(commands_source), sort_keys=True),
                    detected_at_commit,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_engine_detections WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("sandbox engine detection did not persist")
        return result

    def confirm_sandbox_engine_detection(
        self,
        *,
        sandbox_id: str,
        engine: str,
        migrate_commands: Sequence[str],
        seed_commands: Sequence[str],
        commands_source: Mapping[str, str],
        actor: str,
    ) -> dict[str, Any]:
        """Freeze the human-approved project command snapshot for later replay."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandbox_engine_detections
                SET confirmed_engine = ?, migrate_commands_json = ?,
                    seed_commands_json = ?, commands_source = ?, actor = ?,
                    confirmed_at = ?
                WHERE sandbox_id = ?
                """,
                (
                    engine,
                    json.dumps(list(migrate_commands)),
                    json.dumps(list(seed_commands)),
                    json.dumps(dict(commands_source), sort_keys=True),
                    actor,
                    _now(),
                    sandbox_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"sandbox {sandbox_id!r} has no engine detection")
            row = connection.execute(
                "SELECT * FROM sandbox_engine_detections WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("sandbox engine confirmation did not persist")
        return result

    def ensure_sandbox_database(
        self,
        *,
        sandbox_id: str,
        engine: str,
        db_name: str,
        username: str,
        password: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create one durable database intent without rotating its credentials."""
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sandbox_databases(
                    sandbox_id, engine, db_name, username, password,
                    status, provisioned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'provisioning', NULL, ?)
                ON CONFLICT(sandbox_id) DO NOTHING
                """,
                (sandbox_id, engine, db_name, username, password, now),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_databases WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("sandbox database intent did not persist")
        if result["engine"] != engine or result["db_name"] != db_name:
            raise ValueError(
                f"sandbox {sandbox_id!r} already owns database "
                f"{result['engine']}:{result['db_name']}"
            )
        return result, cursor.rowcount == 1

    def sandbox_database(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_databases WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def update_sandbox_database_status(
        self,
        sandbox_id: str,
        *,
        status: str,
        provisioned: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sandbox_databases
                SET status = ?, provisioned_at = CASE WHEN ? THEN ? ELSE provisioned_at END,
                    updated_at = ?
                WHERE sandbox_id = ?
                """,
                (status, int(provisioned), now, now, sandbox_id),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_databases WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        result = _row(row)
        if result is None:
            raise ValueError(f"sandbox {sandbox_id!r} has no database intent")
        return result

    def _delete_sandbox_children(
        self,
        connection: sqlite3.Connection,
        sandbox_id: str,
    ) -> None:
        """Delete non-cascading sandbox children before their parents."""
        connection.execute(
            """
            DELETE FROM work_item_routing WHERE work_item_id IN (
                SELECT id FROM work_items WHERE delegation_id IN (
                    SELECT id FROM delegations WHERE sandbox_id = ?
                )
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            """
            DELETE FROM work_item_runs
            WHERE work_item_id IN (
                SELECT id FROM work_items WHERE delegation_id IN (
                    SELECT id FROM delegations WHERE sandbox_id = ?
                )
            )
            OR delegation_id IN (
                SELECT id FROM delegations WHERE sandbox_id = ?
            )
            OR task_id IN (SELECT id FROM tasks WHERE sandbox_id = ?)
            """,
            (sandbox_id, sandbox_id, sandbox_id),
        )
        connection.execute(
            """
            DELETE FROM work_items WHERE delegation_id IN (
                SELECT id FROM delegations WHERE sandbox_id = ?
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            """
            DELETE FROM delegation_change_requests
            WHERE delegation_id IN (
                SELECT id FROM delegations WHERE sandbox_id = ?
            )
            OR task_id IN (SELECT id FROM tasks WHERE sandbox_id = ?)
            """,
            (sandbox_id, sandbox_id),
        )
        connection.execute(
            """
            DELETE FROM delegation_reviews WHERE delegation_id IN (
                SELECT id FROM delegations WHERE sandbox_id = ?
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            "DELETE FROM delegations WHERE sandbox_id = ?", (sandbox_id,)
        )
        connection.execute(
            "DELETE FROM implementation_contexts WHERE sandbox_id = ?",
            (sandbox_id,),
        )
        connection.execute(
            """
            DELETE FROM planning_plan_revisions WHERE session_id IN (
                SELECT id FROM planning_sessions WHERE sandbox_id = ?
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            """
            DELETE FROM planning_findings WHERE session_id IN (
                SELECT id FROM planning_sessions WHERE sandbox_id = ?
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            """
            DELETE FROM planning_messages WHERE session_id IN (
                SELECT id FROM planning_sessions WHERE sandbox_id = ?
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            "DELETE FROM planning_sessions WHERE sandbox_id = ?", (sandbox_id,)
        )
        connection.execute(
            """
            DELETE FROM assigned_ports WHERE preview_run_id IN (
                SELECT id FROM preview_runs WHERE sandbox_id = ?
            )
            """,
            (sandbox_id,),
        )
        connection.execute(
            "DELETE FROM protected_file_baselines WHERE sandbox_id = ?",
            (sandbox_id,),
        )
        connection.execute("DELETE FROM approvals WHERE sandbox_id = ?", (sandbox_id,))
        connection.execute(
            "DELETE FROM review_rounds WHERE sandbox_id = ?", (sandbox_id,)
        )
        connection.execute("DELETE FROM tasks WHERE sandbox_id = ?", (sandbox_id,))
        connection.execute(
            "DELETE FROM preview_runs WHERE sandbox_id = ?", (sandbox_id,)
        )
        connection.execute(
            """
            DELETE FROM agent_writer_sessions
            WHERE sandbox_id = ?
            OR agent_run_id IN (SELECT id FROM agent_runs WHERE sandbox_id = ?)
            """,
            (sandbox_id, sandbox_id),
        )
        connection.execute("DELETE FROM agent_runs WHERE sandbox_id = ?", (sandbox_id,))
        connection.execute(
            "DELETE FROM shared_database_schemas WHERE sandbox_id = ?",
            (sandbox_id,),
        )
        connection.execute("DELETE FROM events WHERE sandbox_id = ?", (sandbox_id,))

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
            self._delete_sandbox_children(connection, sandbox_id)
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

    def delete_v1_sandbox_manifest(self, sandbox_id: str) -> None:
        """Remove an unprovisioned v1 manifest while preserving its remote project."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM sandboxes WHERE id = ? AND lifecycle_version = 'v1'",
                (sandbox_id,),
            ).fetchone()
            if row is None:
                return
            self._delete_sandbox_children(connection, sandbox_id)
            connection.execute(
                "DELETE FROM sandboxes WHERE id = ? AND lifecycle_version = 'v1'",
                (sandbox_id,),
            )

    def acquire_sandbox_lease(
        self,
        *,
        sandbox_id: str,
        operation: str,
        operation_id: str,
        owner: str,
        allow_writers: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically exclude writers and other lifecycle mutations.

        A null return means the sandbox has no lifecycle state. Destroy passes
        ``allow_writers=True`` and changes lifecycle intent in this transaction.
        """
        now = _now()
        with self._connection() as connection:
            # The read and insert must share this write-intent transaction.
            # Separate store calls can interleave after each _connection block.
            connection.execute("BEGIN IMMEDIATE")
            sandbox = connection.execute(
                """
                SELECT desired_state, lifecycle_status
                FROM sandboxes WHERE id = ?
                """,
                (sandbox_id,),
            ).fetchone()
            if sandbox is None:
                raise ValueError(f"sandbox {sandbox_id!r} is not registered")
            if sandbox["lifecycle_status"] is None:
                return None
            held = connection.execute(
                "SELECT * FROM sandbox_leases WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
            if held is not None:
                raise SandboxLeaseHeldError(sandbox_id, dict(held))
            writers = self._active_writers(connection, sandbox_id)
            if writers and not allow_writers:
                raise SandboxLeaseBlockedByWriterError(sandbox_id, writers)
            if allow_writers:
                if operation != "destroy":
                    raise ValueError(
                        "only destroy may acquire a lease while writers exist"
                    )
                target = SandboxLifecycleStatus.DRAINING
                # A failed writer drain leaves this status in place. Its destroy
                # retry must reassert draining while it acquires the next lease.
                sources = source_statuses(target).union({target})
                placeholders = ", ".join("?" for _ in sources)
                cursor = connection.execute(
                    f"""
                    UPDATE sandboxes
                    SET desired_state = 'destroyed', lifecycle_status = ?,
                        updated_at = ?
                    WHERE id = ? AND lifecycle_status IN ({placeholders})
                    """,
                    (
                        target.value,
                        now,
                        sandbox_id,
                        *(status.value for status in sources),
                    ),
                )
                if cursor.rowcount != 1:
                    raise SandboxAdmissionError(
                        f"Sandbox '{sandbox_id}' cannot enter draining from its current status"
                    )
            connection.execute(
                """
                INSERT INTO sandbox_leases(
                    sandbox_id, operation, operation_id, owner,
                    acquired_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sandbox_id, operation, operation_id, owner, now, now),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_leases WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    @contextmanager
    def sandbox_lifecycle_lease(
        self,
        *,
        sandbox_id: str,
        operation: str,
        operation_id: str,
        owner: str,
        allow_writers: bool = False,
    ) -> Iterator[dict[str, Any] | None]:
        lease = self.acquire_sandbox_lease(
            sandbox_id=sandbox_id,
            operation=operation,
            operation_id=operation_id,
            owner=owner,
            allow_writers=allow_writers,
        )
        try:
            yield lease
        finally:
            if lease is not None:
                self.release_sandbox_lease(sandbox_id, operation_id)

    def sandbox_lease(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_leases WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def heartbeat_sandbox_lease(self, sandbox_id: str, operation_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE sandbox_leases SET heartbeat_at = ?
                WHERE sandbox_id = ? AND operation_id = ?
                """,
                (_now(), sandbox_id, operation_id),
            )
        return cursor.rowcount == 1

    def release_sandbox_lease(self, sandbox_id: str, operation_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM sandbox_leases
                WHERE sandbox_id = ? AND operation_id = ?
                """,
                (sandbox_id, operation_id),
            )
        return cursor.rowcount == 1

    def reclaim_sandbox_leases(self, *, stale_before: str) -> int:
        """Reclaim leases whose lifecycle settled or heartbeat expired."""
        settled_statuses = {
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION.value,
            SandboxLifecycleStatus.READY.value,
            SandboxLifecycleStatus.DATABASE_FAILED.value,
            SandboxLifecycleStatus.DEGRADED.value,
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT lease.*, sandbox.lifecycle_status, sandbox.desired_state
                FROM sandbox_leases AS lease
                JOIN sandboxes AS sandbox ON sandbox.id = lease.sandbox_id
                """
            ).fetchall()
            reclaimable = [
                (str(row["sandbox_id"]), str(row["operation_id"]))
                for row in rows
                if str(row["heartbeat_at"]) <= stale_before
                or row["lifecycle_status"] is None
                or str(row["lifecycle_status"]) in settled_statuses
            ]
            connection.executemany(
                """
                DELETE FROM sandbox_leases
                WHERE sandbox_id = ? AND operation_id = ?
                """,
                reclaimable,
            )
        return len(reclaimable)

    def record_sandbox_resource(self, sandbox_id: str, *, kind: str, name: str) -> None:
        if kind not in {"volume", "container", "network"}:
            raise ValueError(f"unsupported sandbox resource kind {kind!r}")
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sandbox_resources(sandbox_id, kind, name) VALUES (?, ?, ?)",
                (sandbox_id, kind, name),
            )

    def sandbox_resources(self, sandbox_id: str) -> list[dict[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT kind, name FROM sandbox_resources WHERE sandbox_id = ? ORDER BY kind, name",
                (sandbox_id,),
            ).fetchall()
        return [{"kind": str(row["kind"]), "name": str(row["name"])} for row in rows]

    def sandbox_tombstone(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM sandbox_tombstones WHERE sandbox_id = ?", (sandbox_id,)
            ).fetchone()
        return _row(row)

    def write_sandbox_tombstone(
        self, sandbox_id: str, *, reason: str, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        now = _now()
        payload = json.dumps(dict(manifest), sort_keys=True, default=str)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sandbox_tombstones(sandbox_id, destroyed_at, reason, manifest_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sandbox_id) DO NOTHING
                """,
                (sandbox_id, now, reason, payload),
            )
            row = connection.execute(
                "SELECT * FROM sandbox_tombstones WHERE sandbox_id = ?", (sandbox_id,)
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("sandbox tombstone did not persist")
        return result

    def unexpected_resources(self) -> list[dict[str, Any]]:
        """Return the latest startup report for each discoverable orphan."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, payload_json, created_at
                FROM events
                WHERE kind = 'controller.unexpected_resource'
                ORDER BY id DESC
                """
            ).fetchall()
        resources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            resource = payload.get("resource")
            kind = payload.get("resource_kind")
            name = payload.get("resource_name")
            if (
                not isinstance(resource, str)
                or not isinstance(kind, str)
                or not isinstance(name, str)
                or resource in seen
            ):
                continue
            seen.add(resource)
            resources.append(
                {
                    "resource": resource,
                    "kind": kind,
                    "name": name,
                    "reported_at": str(row["created_at"]),
                }
            )
        return resources
