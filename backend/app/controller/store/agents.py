import sqlite3
from typing import Any

from app.controller.store.lifecycle_status import SandboxLifecycleStatus

from ._shared import _now, _row
from .errors import (
    ActiveAgentRunExists,
    AgentWriterSessionExists,
    SandboxWriterAdmissionError,
)
from .migrations import _violates


class AgentsMixin:
    """Owns agent_runs and agent_writer_sessions tables."""

    def active_agent(self, sandbox_id: str) -> dict[str, Any] | None:
        return self._active_run(
            "agent_runs",
            sandbox_id,
            ("created", "running", "replacing", "stopping"),
        )

    def agent_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _row(row)

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
            connection.execute("BEGIN IMMEDIATE")
            self._assert_writer_admission(connection, sandbox_id)
            try:
                connection.execute(
                    """
                    INSERT INTO agent_runs(
                        id, sandbox_id, container_id, provider, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, sandbox_id, container_id, provider, status, created_at),
                )
            except sqlite3.IntegrityError as error:
                if not _violates(error, "one_active_agent_per_sandbox"):
                    raise
                raise ActiveAgentRunExists(sandbox_id) from error
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

    def open_agent_writer_session(
        self,
        *,
        session_id: str,
        sandbox_id: str,
        agent_run_id: str,
        kind: str,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_writer_admission(connection, sandbox_id)
            try:
                connection.execute(
                    """
                    INSERT INTO agent_writer_sessions(
                        id, sandbox_id, agent_run_id, kind,
                        started_at, ended_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (session_id, sandbox_id, agent_run_id, kind, now, now),
                )
            except sqlite3.IntegrityError as error:
                if not _violates(error, "one_open_agent_writer_session_per_sandbox"):
                    raise
                raise AgentWriterSessionExists(sandbox_id) from error

    def heartbeat_agent_writer_session(self, session_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_writer_sessions
                SET heartbeat_at = ?
                WHERE id = ? AND ended_at IS NULL
                """,
                (_now(), session_id),
            )
        return cursor.rowcount == 1

    def close_agent_writer_session(self, session_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_writer_sessions
                SET ended_at = ?
                WHERE id = ? AND ended_at IS NULL
                """,
                (_now(), session_id),
            )
        return cursor.rowcount == 1

    def close_open_agent_writer_sessions(self) -> int:
        """Close sessions no websocket can still own after a backend restart."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_writer_sessions
                SET ended_at = ?
                WHERE ended_at IS NULL
                """,
                (_now(),),
            )
        return cursor.rowcount

    def agent_writer_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_writer_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _row(row)

    def active_writers(self, sandbox_id: str) -> list[dict[str, Any]]:
        """Return every active writer class without treating agent existence as work."""
        # Planning sessions are intentionally absent. Their runner mounts the
        # workspace read-only, so they cannot participate in writer exclusion.
        with self._connection() as connection:
            return self._active_writers(connection, sandbox_id)

    @staticmethod
    def _active_writers(
        connection: sqlite3.Connection,
        sandbox_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT 'task' AS writer_class, id AS writer_id, status, NULL AS kind,
                   NULL AS agent_run_id
            FROM tasks
            WHERE sandbox_id = ?
              AND status IN ('preparing', 'open', 'reported', 'previewing', 'review')
            UNION ALL
            SELECT 'preview', id, status, kind, NULL
            FROM preview_runs
            WHERE sandbox_id = ?
              AND status IN ('preparing', 'running', 'restarting', 'rebuilding', 'stopping')
            UNION ALL
            SELECT 'delegation', id, status, NULL, NULL
            FROM delegations
            WHERE sandbox_id = ? AND status IN ('ready', 'running', 'halted')
            UNION ALL
            SELECT 'agent_writer_session', id, 'open', kind, agent_run_id
            FROM agent_writer_sessions
            WHERE sandbox_id = ? AND ended_at IS NULL
            """,
            (sandbox_id, sandbox_id, sandbox_id, sandbox_id),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _assert_writer_admission(
        connection: sqlite3.Connection,
        sandbox_id: str,
    ) -> None:
        sandbox = connection.execute(
            """
            SELECT desired_state, lifecycle_status
            FROM sandboxes WHERE id = ?
            """,
            (sandbox_id,),
        ).fetchone()
        if sandbox is None:
            raise ValueError(f"sandbox {sandbox_id!r} is not registered")
        lifecycle_status = sandbox["lifecycle_status"]
        # Legacy rows deliberately have no lifecycle state. Their established
        # writer behavior stays independent of the v1 lease mechanism.
        if lifecycle_status is None:
            return
        lease = connection.execute(
            "SELECT * FROM sandbox_leases WHERE sandbox_id = ?",
            (sandbox_id,),
        ).fetchone()
        if lease is not None:
            raise SandboxWriterAdmissionError(sandbox_id, lease=dict(lease))
        desired_state = sandbox["desired_state"]
        if lifecycle_status != SandboxLifecycleStatus.READY or desired_state != "active":
            raise SandboxWriterAdmissionError(
                sandbox_id,
                lifecycle_status=SandboxLifecycleStatus(str(lifecycle_status)),
                desired_state=str(desired_state),
            )
