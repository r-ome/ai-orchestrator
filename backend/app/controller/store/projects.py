from collections.abc import Mapping
from typing import Any

from ._shared import _now, _row
from .errors import SandboxLeaseHeldError


class ProjectsMixin:
    """Owns projects, project_secrets, and project_mirror_locks tables."""

    def register_v1_project(
        self,
        *,
        project_id: str,
        remote_url: str,
        default_branch: str,
        mirror_volume: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Register a remote-keyed project without changing an existing project ID."""
        # Importing at the persistence boundary makes credential-free storage an
        # invariant, even when a future caller bypasses a service-layer helper.
        from app.platform.remote import normalize_remote_url

        normalized_remote_url = normalize_remote_url(remote_url)
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, remote_url, default_branch, mirror_volume, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(remote_url) WHERE remote_url IS NOT NULL DO NOTHING
                """,
                (
                    project_id,
                    normalized_remote_url,
                    default_branch,
                    mirror_volume,
                    created_at or now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE remote_url = ?",
                (normalized_remote_url,),
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("v1 project registration did not persist a project")
        return result

    def set_v1_project_mirror(
        self,
        *,
        project_id: str,
        default_branch: str,
        mirror_volume: str,
    ) -> dict[str, Any]:
        """Persist canonical mirror facts after a successful fetch."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET default_branch = ?, mirror_volume = ?, mirror_fetched_at = ?
                WHERE id = ? AND remote_url IS NOT NULL
                """,
                (default_branch, mirror_volume, _now(), project_id),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("v1 project mirror update did not persist a project")
        return result

    def record_v1_project_mirror_fetch(self, *, project_id: str) -> dict[str, Any]:
        """Record a successful canonical fetch without changing mirror identity."""
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET mirror_fetched_at = ?
                WHERE id = ? AND remote_url IS NOT NULL
                """,
                (_now(), project_id),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("v1 project mirror fetch update did not persist a project")
        return result

    def project(self, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return _row(row)

    def v1_projects(self) -> list[dict[str, Any]]:
        """List remote-keyed projects, each with its v1 sandbox count.

        A remote URL is what makes a project a project. Rows without one do
        not appear here.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT projects.*, (
                    SELECT COUNT(*) FROM sandboxes
                    WHERE sandboxes.project_id = projects.id
                    AND sandboxes.lifecycle_version = 'v1'
                ) AS sandbox_count
                FROM projects
                WHERE remote_url IS NOT NULL
                ORDER BY remote_url
                """
            ).fetchall()
        return [row for row in (_row(row) for row in rows) if row is not None]

    def v1_project(self, project_id: str) -> dict[str, Any] | None:
        """Return one remote-keyed project with its v1 sandbox count."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT projects.*, (
                    SELECT COUNT(*) FROM sandboxes
                    WHERE sandboxes.project_id = projects.id
                    AND sandboxes.lifecycle_version = 'v1'
                ) AS sandbox_count
                FROM projects
                WHERE id = ? AND remote_url IS NOT NULL
                """,
                (project_id,),
            ).fetchone()
        return _row(row)

    def sandboxes_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sandboxes WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [row for row in (_row(row) for row in rows) if row is not None]

    def delete_v1_project(self, project_id: str) -> None:
        """Delete a remote-keyed project that has no sandboxes left.

        Sandbox teardown stays exclusively at `DELETE /sandboxes/{id}`, which
        is the only path that takes the lifecycle lease and drains writers.
        This refuses rather than cascading, so a project can never become a
        second, leaseless way to destroy a sandbox.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM projects WHERE id = ? AND remote_url IS NOT NULL",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no remote project {project_id!r}")
            remaining = connection.execute(
                "SELECT COUNT(*) FROM sandboxes WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            if remaining:
                raise ValueError(
                    f"project {project_id!r} still has {remaining} sandbox(es); "
                    "remove each sandbox first"
                )
            connection.execute(
                "DELETE FROM project_secrets WHERE project_id = ?", (project_id,)
            )
            connection.execute(
                "DELETE FROM project_mirror_locks WHERE project_id = ?", (project_id,)
            )
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def acquire_project_mirror_lock(
        self,
        *,
        project_id: str,
        operation: str,
        operation_id: str,
        owner: str,
    ) -> dict[str, Any]:
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise ValueError(f"project {project_id!r} is not registered")
            held = connection.execute(
                "SELECT * FROM project_mirror_locks WHERE project_id = ?", (project_id,)
            ).fetchone()
            if held is not None:
                raise SandboxLeaseHeldError(project_id, dict(held))
            connection.execute(
                """
                INSERT INTO project_mirror_locks(
                    project_id, operation, operation_id, owner, acquired_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, operation, operation_id, owner, now, now),
            )
            row = connection.execute(
                "SELECT * FROM project_mirror_locks WHERE project_id = ?", (project_id,)
            ).fetchone()
        result = _row(row)
        if result is None:
            raise RuntimeError("project mirror lock did not persist")
        return result

    def project_mirror_lock(self, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM project_mirror_locks WHERE project_id = ?", (project_id,)
            ).fetchone()
        return _row(row)

    def release_project_mirror_lock(self, project_id: str, operation_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM project_mirror_locks WHERE project_id = ? AND operation_id = ?",
                (project_id, operation_id),
            )
        return cursor.rowcount == 1

    def reclaim_project_mirror_locks(self, *, stale_before: str) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM project_mirror_locks WHERE heartbeat_at <= ?", (stale_before,)
            )
        return cursor.rowcount

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
