import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from ._shared import _now, _row
from .errors import OpenTaskExists
from .migrations import _violates


class TasksMixin:
    """Owns tasks table."""

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
        """Claims the sandbox's single open-task slot, or raises OpenTaskExists.

        The one_open_task_per_sandbox partial index is the only thing that
        decides the race, exactly as one_active_agent_per_sandbox does for
        coding agents. Callers must insert before touching git, so a losing
        caller has not yet changed the sandbox.
        """
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_writer_admission(connection, sandbox_id)
            try:
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
            except sqlite3.IntegrityError as error:
                if not _violates(error, "one_open_task_per_sandbox"):
                    raise
                raise OpenTaskExists(sandbox_id) from error
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

    def complete_task_preparation(
        self,
        *,
        task_id: str,
        base_branch: str,
        base_commit: str,
    ) -> bool:
        """Publish prepared base facts and open the task in one guarded write."""
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET base_branch = ?, base_commit = ?, status = 'open', updated_at = ?
                WHERE id = ? AND status = 'preparing'
                """,
                (base_branch, base_commit, now, task_id),
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
                payload={"status": "open", "head_commit": None},
            )
        return True

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

