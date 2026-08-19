import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from app.controller.config import ControllerSettings, get_controller_settings
from app.sandboxes.models import SandboxLifecycleStatus, source_statuses

from ._shared import _ensure_immutable_manifest_value, _json, _now, _row
from .errors import (
    ActiveAgentRunExists,
    AgentWriterSessionExists,
    ChangeRequestRunning,
    DelegationActive,
    OpenTaskExists,
    ReviewGenerating,
    RevisionTaken,
    RunActive,
    SandboxAdmissionError,
    SandboxLeaseBlockedByWriterError,
    SandboxLeaseHeldError,
    SandboxWriterAdmissionError,
    SlotTaken,
)
from .migrations import MIGRATIONS, _add_column, _violates
from .agents import AgentsMixin
from .connection import ConnectionMixin
from .events import EventsMixin
from .previews import PreviewsMixin
from .projects import ProjectsMixin
from .queries import _PLANNING_SESSION_FEATURE_FACTS_QUERY
from .reviews import ReviewsMixin
from .tasks import TasksMixin
from .schema import (
    FIRST_V1_MIGRATION,
    INITIAL_MIGRATION,
    _CONTEXT_UPDATABLE_COLUMNS,
    _RUN_UPDATABLE_COLUMNS,
)


class ControllerStore(
    ConnectionMixin,
    ProjectsMixin,
    TasksMixin,
    ReviewsMixin,
    AgentsMixin,
    PreviewsMixin,
    EventsMixin,
):
    """Serialized SQLite access for controller-owned intent and audit state."""

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
        clarifier_model: str | None = None,
        planner_model: str | None = None,
        reviewer_model: str | None = None,
        reviewer_reasoning_effort: str | None = None,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO planning_sessions(
                    id, project_id, sandbox_id, project_name, title, status,
                    clarifier_provider, planner_provider, reviewer_provider,
                    clarifier_model, planner_model, reviewer_model,
                    reviewer_reasoning_effort, credential_profile, max_review_turns,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    clarifier_model,
                    planner_model,
                    reviewer_model,
                    reviewer_reasoning_effort,
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
                _PLANNING_SESSION_FEATURE_FACTS_QUERY
                + """
                WHERE session.project_id = ?
                ORDER BY session.created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def planning_session_with_feature_facts(self, session_id: str) -> dict[str, Any] | None:
        """Return one planning session with facts used for its feature status."""
        with self._connection() as connection:
            row = connection.execute(
                _PLANNING_SESSION_FEATURE_FACTS_QUERY
                + """
                WHERE session.id = ?
                """,
                (session_id,),
            ).fetchone()
        return _row(row)

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

    def claim_delegation_revision(
        self,
        delegation: Mapping[str, Any],
        items: Iterable[Mapping[str, Any]],
    ) -> int:
        """Claim the next delegation revision and write its work items."""
        now = _now()
        values = dict(delegation)
        rows = list(items)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_writer_admission(
                connection,
                str(values["sandbox_id"]),
            )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS highest
                FROM delegations WHERE session_id = ?
                """,
                (values["session_id"],),
            ).fetchone()
            revision = int(row["highest"]) + 1
            values["revision"] = revision
            try:
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
                    {**values, "created_at": now, "updated_at": now},
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
                    [{**dict(item), "created_at": now} for item in rows],
                )
            except sqlite3.IntegrityError as error:
                if _violates(error, "one_delegation_revision_per_session"):
                    raise RevisionTaken(str(values["session_id"])) from error
                if _violates(error, "one_active_delegation_per_sandbox"):
                    raise DelegationActive(str(values["sandbox_id"])) from error
                raise
            self._event(
                connection,
                sandbox_id=str(values["sandbox_id"]),
                run_id=str(values["id"]),
                kind="delegation.created",
                payload={"revision": revision, "work_items": len(rows)},
            )
        return revision

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

    def latest_completed_delegation_review_for_sandbox(
        self, sandbox_id: str
    ) -> dict[str, Any] | None:
        """Return the newest completed review with a recorded target.

        Approval stays in ``result_json`` because the review result is the
        authoritative review artifact. Publish validates it before network Git.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT review.*
                FROM delegation_reviews AS review
                JOIN delegations AS delegation ON delegation.id = review.delegation_id
                WHERE delegation.sandbox_id = ?
                  AND review.status = 'completed'
                  AND review.base_branch IS NOT NULL
                  AND review.base_commit IS NOT NULL
                  AND review.head_commit IS NOT NULL
                ORDER BY review.settled_at DESC, review.revision DESC
                LIMIT 1
                """,
                (sandbox_id,),
            ).fetchone()
        return _row(row)

    def claim_delegation_review(self, review: Mapping[str, Any]) -> int:
        """Claim the next review revision and create its generating row."""
        now = _now()
        values = dict(review)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS highest
                FROM delegation_reviews WHERE delegation_id = ?
                """,
                (values["delegation_id"],),
            ).fetchone()
            revision = int(row["highest"]) + 1
            values["revision"] = revision
            try:
                connection.execute(
                    """
                    INSERT INTO delegation_reviews(
                        id, delegation_id, revision, status, provider, model,
                        base_branch, base_commit, head_commit,
                        created_at, updated_at
                    ) VALUES (
                        :id, :delegation_id, :revision, :status, :provider, :model,
                        :base_branch, :base_commit, :head_commit,
                        :created_at, :updated_at
                    )
                    """,
                    {
                        **values,
                        "base_branch": values.get("base_branch"),
                        "base_commit": values.get("base_commit"),
                        "head_commit": values.get("head_commit"),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            except sqlite3.IntegrityError as error:
                if _violates(error, "one_review_revision_per_delegation"):
                    raise RevisionTaken(str(values["delegation_id"])) from error
                if _violates(error, "one_generating_review_per_delegation"):
                    raise ReviewGenerating(str(values["delegation_id"])) from error
                raise
            self._event(
                connection,
                sandbox_id=None,
                run_id=str(values["id"]),
                kind="delegation_review.generating",
                payload={"revision": revision},
            )
        return revision

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

    def pin_delegation_review_target(
        self,
        review_id: str,
        *,
        base_branch: str,
        base_commit: str,
        head_commit: str,
    ) -> dict[str, Any] | None:
        """Pins a generating review before its read-only model turn starts."""
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_reviews
                SET base_branch = ?, base_commit = ?, head_commit = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'generating'
                  AND base_commit IS NULL AND head_commit IS NULL
                """,
                (base_branch, base_commit, head_commit, now, review_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM delegation_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
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

    def mark_delegation_review_source_merged(
        self,
        review_id: str,
    ) -> dict[str, Any] | None:
        """Records one idempotent delivery of the reviewed commit to its source."""
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_reviews
                SET source_merged_at = COALESCE(source_merged_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'completed'
                """,
                (now, now, review_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM delegation_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
            updated = _row(row)
            self._event(
                connection,
                sandbox_id=None,
                run_id=review_id,
                kind="delegation_review.source_merged",
                payload={"head_commit": (updated or {}).get("head_commit")},
            )
        return updated

    def delegation_review(self, review_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        return _row(row)

    def claim_delegation_change_request(self, request: Mapping[str, Any]) -> int:
        """Claim the next change revision and create its running row."""
        now = _now()
        values = dict(request)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS highest
                FROM delegation_change_requests WHERE delegation_id = ?
                """,
                (values["delegation_id"],),
            ).fetchone()
            revision = int(row["highest"]) + 1
            values["revision"] = revision
            try:
                connection.execute(
                    """
                    INSERT INTO delegation_change_requests(
                        id, delegation_id, revision, status, instructions,
                        provider, model, task_id, prompt, created_at, updated_at
                    ) VALUES (
                        :id, :delegation_id, :revision, :status, :instructions,
                        :provider, :model, :task_id, :prompt, :created_at, :updated_at
                    )
                    """,
                    {
                        **values,
                        "prompt": values.get("prompt"),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            except sqlite3.IntegrityError as error:
                if _violates(error, "one_change_revision_per_delegation"):
                    raise RevisionTaken(str(values["delegation_id"])) from error
                if _violates(error, "one_running_change_per_delegation"):
                    raise ChangeRequestRunning(str(values["delegation_id"])) from error
                raise
            self._event(
                connection,
                sandbox_id=None,
                run_id=str(values["id"]),
                kind="change_request.running",
                payload={"revision": revision},
            )
        return revision

    def settle_delegation_change_request(
        self,
        request_id: str,
        *,
        to_status: str,
        verification_json: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE delegation_change_requests
                SET status = ?, verification_json = ?, error = ?,
                    updated_at = ?, settled_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (to_status, verification_json, error, now, now, request_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM delegation_change_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            self._event(
                connection,
                sandbox_id=None,
                run_id=request_id,
                kind=f"change_request.{to_status}",
                payload={"status": to_status},
            )
        return _row(row)

    def delegation_change_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_change_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return _row(row)

    def delegation_change_requests(self, delegation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegation_change_requests
                WHERE delegation_id = ? ORDER BY revision
                """,
                (delegation_id,),
            ).fetchall()
        return [_row(row) for row in rows if row is not None]

    def complete_awaiting_delegation_changes(
        self,
        delegation_id: str,
        *,
        review_id: str,
    ) -> int:
        """Complete held changes after the current whole-feature review approves."""
        now = _now()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id FROM delegation_change_requests
                WHERE delegation_id = ? AND status = 'awaiting_review'
                ORDER BY revision
                """,
                (delegation_id,),
            ).fetchall()
            if not rows:
                return 0
            connection.execute(
                """
                UPDATE delegation_change_requests
                SET status = 'completed', updated_at = ?, settled_at = ?
                WHERE delegation_id = ? AND status = 'awaiting_review'
                """,
                (now, now, delegation_id),
            )
            for row in rows:
                request_id = str(row["id"])
                self._event(
                    connection,
                    sandbox_id=None,
                    run_id=request_id,
                    kind="change_request.completed",
                    payload={"status": "completed", "review_id": review_id},
                )
        return len(rows)

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

    def claim_work_item_run(self, run: Mapping[str, Any]) -> int:
        """Append one attempt and claim the delegation's running slot."""
        now = _now()
        values = dict(run)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) AS highest
                FROM work_item_runs WHERE work_item_id = ?
                """,
                (values["work_item_id"],),
            ).fetchone()
            attempt = int(row["highest"]) + 1
            values["attempt"] = attempt
            try:
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
                    {**values, "created_at": now, "updated_at": now},
                )
            except sqlite3.IntegrityError as error:
                if _violates(error, "one_attempt_number_per_work_item"):
                    raise RevisionTaken(str(values["work_item_id"])) from error
                if _violates(error, "one_running_run_per_delegation"):
                    raise RunActive(str(values["delegation_id"])) from error
                raise
            self._event(
                connection,
                sandbox_id=None,
                run_id=str(values["id"]),
                kind="work_item_run.running",
                payload={
                    "work_item_id": values["work_item_id"],
                    "attempt": attempt,
                },
            )
        return attempt

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
            "delegation_change_requests": (
                "SELECT * FROM delegation_change_requests WHERE status = 'running'"
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

    # Lock order for callers that need both locks is: sandbox lease first,
    # then this project mirror lock.  The transaction makes check + insert one
    # admission operation; do not split these into separate store calls.
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


__all__ = [
    "ActiveAgentRunExists",
    "AgentWriterSessionExists",
    "ChangeRequestRunning",
    "ControllerStore",
    "DelegationActive",
    "FIRST_V1_MIGRATION",
    "INITIAL_MIGRATION",
    "MIGRATIONS",
    "OpenTaskExists",
    "ReviewGenerating",
    "RevisionTaken",
    "RunActive",
    "SandboxAdmissionError",
    "SandboxLeaseBlockedByWriterError",
    "SandboxLeaseHeldError",
    "SandboxWriterAdmissionError",
    "SlotTaken",
    "controller_store_for_settings",
    "get_controller_store",
]
