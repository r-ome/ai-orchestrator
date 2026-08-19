from collections.abc import Iterable, Mapping
from typing import Any

from ._shared import _json, _now, _row
from .queries import _PLANNING_SESSION_FEATURE_FACTS_QUERY


class PlanningMixin:
    """Owns planning sessions, turns, messages, and plan revisions."""

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
