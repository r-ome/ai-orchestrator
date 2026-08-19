import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from ._shared import _now, _row
from .errors import (
    ChangeRequestRunning,
    DelegationActive,
    ReviewGenerating,
    RevisionTaken,
    RunActive,
)
from .migrations import _violates
from .schema import _CONTEXT_UPDATABLE_COLUMNS, _RUN_UPDATABLE_COLUMNS


class ImplementationMixin:
    """Owns implementation contexts, delegations, and work-item runs."""

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
