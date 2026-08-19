import sqlite3
from pathlib import Path

import pytest

import app.controller.store as store_module
import app.controller.store.migrations as migrations_module
from app.controller.store import ControllerStore

SANDBOX_LIFECYCLE_COLUMNS = {
    "lifecycle_version": ("TEXT", 0, None),
    "feature_key": ("TEXT", 0, None),
    "feature_title": ("TEXT", 0, None),
    "desired_state": ("TEXT", 0, None),
    "lifecycle_status": ("TEXT", 0, None),
    "last_error": ("TEXT", 0, None),
    "base_ref": ("TEXT", 0, None),
    "created_base_commit": ("TEXT", 0, None),
    "current_base_commit": ("TEXT", 0, None),
    "pending_base_commit": ("TEXT", 0, None),
    "feature_branch": ("TEXT", 0, None),
    "agent_provider": ("TEXT", 0, None),
    "network_policy": ("TEXT", 0, None),
    "db_engine": ("TEXT", 0, None),
    "db_name": ("TEXT", 0, None),
    "schema_baseline_hash": ("TEXT", 0, None),
    "db_data_volume": ("TEXT", 0, None),
    "publish_remote": ("TEXT", 0, None),
    "remote_branch": ("TEXT", 0, None),
    "pr_requested": ("INTEGER", 1, "0"),
}

PROJECT_COLUMNS = {
    "id": ("TEXT", 0, None),
    "source_path": ("TEXT", 0, None),
    "remote_url": ("TEXT", 0, None),
    "default_branch": ("TEXT", 0, None),
    "mirror_volume": ("TEXT", 0, None),
    "mirror_fetched_at": ("TEXT", 0, None),
    "created_at": ("TEXT", 1, None),
}


def _seed_database_at_versions(database_path: Path, versions: list[int]) -> None:
    """Build a pre-upgrade database holding one project and one sandbox.

    ``versions`` are stamped into ``schema_migrations`` without running
    anything. Versions 2 to 17 were squashed into ``INITIAL_MIGRATION`` and no
    longer exist as callables, so stamping is the only way to reproduce a
    database that predates the squash.
    """
    with sqlite3.connect(database_path) as connection:
        connection.executescript(store_module.INITIAL_MIGRATION)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(version, "2026-08-11T00:00:00+00:00") for version in versions],
        )
        connection.execute(
            """
            INSERT INTO projects(id, source_path, created_at)
            VALUES ('project-1', '/projects/sample', '2026-08-11T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO sandboxes(
                id, project_id, project_name, volume_name, status, created_at, updated_at
            ) VALUES (
                'sandbox-1', 'project-1', 'sample', 'sample-volume', 'ready',
                '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00'
            )
            """
        )


def _database_ids(database_path: Path) -> tuple[list[str], list[str]]:
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        project_ids = [
            str(row[0])
            for row in connection.execute("SELECT id FROM projects ORDER BY id")
        ]
        sandbox_ids = [
            str(row[0])
            for row in connection.execute("SELECT id FROM sandboxes ORDER BY id")
        ]
    return project_ids, sandbox_ids


def _assert_database_is_consistent(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _sandbox_column_schema(
    database_path: Path,
) -> dict[str, tuple[str, int, str | None]]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[1]): (str(row[2]), int(row[3]), row[4])
            for row in connection.execute("PRAGMA table_info(sandboxes)")
        }


def _project_schema(database_path: Path) -> dict[str, tuple[str, int, str | None]]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[1]): (str(row[2]), int(row[3]), row[4])
            for row in connection.execute("PRAGMA table_info(projects)")
        }


def _project_index_sql(database_path: Path) -> dict[str, str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'projects'"
            )
        }


def _create_legacy_sandbox_database(database_path: Path) -> ControllerStore:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(store_module.INITIAL_MIGRATION)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            ("2026-08-11T00:00:00+00:00",),
        )
        connection.execute(
            """
            INSERT INTO projects(id, source_path, created_at)
            VALUES ('project-1', '/projects/sample', '2026-08-11T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO sandboxes(
                id, project_id, project_name, volume_name, status, created_at, updated_at
            ) VALUES (
                'sandbox-1', 'project-1', 'sample', 'sample-volume', 'ready',
                '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00'
            )
            """
        )
    return ControllerStore(database_path)


def _store_with_sandbox(
    tmp_path: Path,
    *,
    sandbox_id: str,
    project_id: str,
) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_v1_project(
        project_id=project_id,
        remote_url=f"https://example.test/{project_id}.git",
        default_branch="main",
        mirror_volume=f"{project_id}-mirror",
        created_at="2026-08-12T00:00:00Z",
    )
    store.register_v1_sandbox(
        sandbox_id=sandbox_id,
        project_id=project_id,
        project_name=project_id,
        volume_name=f"{sandbox_id}-volume",
        created_at="2026-08-12T00:00:00Z",
    )
    return store


def _seed_planning_delegation_tree(
    store: ControllerStore,
    *,
    sandbox_id: str,
    project_id: str,
) -> None:
    """Create every non-cascading planning and delegation child for one sandbox."""
    prefix = sandbox_id
    now = "2026-08-12T00:00:00Z"
    session_id = f"{prefix}-session"
    context_id = f"{prefix}-context"
    delegation_id = f"{prefix}-delegation"
    task_id = f"{prefix}-task"
    work_item_id = f"{prefix}-work-item"
    with store._connection() as connection:
        connection.execute(
            """
            INSERT INTO planning_sessions(
                id, project_id, sandbox_id, project_name, title, status,
                clarifier_provider, planner_provider, reviewer_provider,
                credential_profile, max_review_turns, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Plan', 'plan_ready', 'test', 'test', 'test',
                      'default', 1, ?, ?)
            """,
            (session_id, project_id, sandbox_id, project_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO planning_messages(session_id, sequence, role, text, created_at)
            VALUES (?, 1, 'user', 'Plan this work', ?)
            """,
            (session_id, now),
        )
        connection.execute(
            """
            INSERT INTO planning_findings(
                session_id, finding_id, severity, text, status,
                raised_in_round, last_seen_round, updated_at
            ) VALUES (?, 'finding-1', 'warning', 'Check this', 'open', 1, 1, ?)
            """,
            (session_id, now),
        )
        connection.execute(
            """
            INSERT INTO planning_plan_revisions(
                session_id, revision, plan_json, plan_markdown, created_at
            ) VALUES (?, 1, '{}', 'Plan', ?)
            """,
            (session_id, now),
        )
        connection.execute(
            """
            INSERT INTO implementation_contexts(
                id, session_id, sandbox_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'ready', ?, ?)
            """,
            (context_id, session_id, sandbox_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, sandbox_id, branch, base_commit, status, created_at, updated_at
            ) VALUES (?, ?, 'task/branch', '0000000000000000000000000000000000000000',
                      'failed', ?, ?)
            """,
            (task_id, sandbox_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO delegations(
                id, session_id, sandbox_id, context_id, revision, status, created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, 1, 'completed', ?, ?)
            """,
            (delegation_id, session_id, sandbox_id, context_id, now, now),
        )
        connection.execute(
            """
                INSERT INTO work_items(
                    id, delegation_id, key, position, title, objective, scope,
                    dependencies_json, files_json, symbols_json, write_scope_json,
                    acceptance_criteria_json, verification_json, complexity,
                    architecture_json, risks_json, created_at
                ) VALUES (?, ?, 'item-1', 1, 'Item', 'Do the work', 'src', '[]', '[]',
                          '[]', '[]', '[]', '[]', 'small', '[]', '[]', ?)
            """,
            (work_item_id, delegation_id, now),
        )
        connection.execute(
            """
            INSERT INTO work_item_routing(work_item_id, provider, model, actor, updated_at)
            VALUES (?, 'test', 'test-model', 'tester', ?)
            """,
            (work_item_id, now),
        )
        connection.execute(
            """
            INSERT INTO work_item_runs(
                id, work_item_id, delegation_id, attempt, status, task_id, created_at,
                updated_at
            ) VALUES (?, ?, ?, 1, 'completed', ?, ?, ?)
            """,
            (f"{prefix}-work-item-run", work_item_id, delegation_id, task_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO delegation_reviews(
                id, delegation_id, revision, status, created_at, updated_at
            ) VALUES (?, ?, 1, 'completed', ?, ?)
            """,
            (f"{prefix}-delegation-review", delegation_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO delegation_change_requests(
                id, delegation_id, revision, status, instructions, provider, model,
                task_id, created_at, updated_at
            ) VALUES (?, ?, 1, 'completed', 'Make a change', 'test', 'test-model', ?, ?, ?)
            """,
            (f"{prefix}-change-request", delegation_id, task_id, now, now),
        )


def test_planning_sessions_for_project_includes_latest_feature_facts(
    tmp_path: Path,
) -> None:
    store = _store_with_sandbox(
        tmp_path,
        sandbox_id="sandbox-feature-status",
        project_id="project-feature-status",
    )
    now = "2026-08-14T00:00:00Z"
    with store._connection() as connection:
        connection.execute(
            """
            INSERT INTO planning_sessions(
                id, project_id, sandbox_id, project_name, title, status,
                clarifier_provider, planner_provider, reviewer_provider,
                credential_profile, max_review_turns, created_at, updated_at
            ) VALUES (?, ?, ?, 'Feature status', 'Plan', 'plan_ready', 'test', 'test',
                      'test', 'default', 1, ?, ?)
            """,
            (
                "session-feature-status",
                "project-feature-status",
                "sandbox-feature-status",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO implementation_contexts(
                id, session_id, sandbox_id, status, created_at, updated_at
            ) VALUES ('context-feature-status', 'session-feature-status',
                      'sandbox-feature-status', 'ready', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO delegations(
                id, session_id, sandbox_id, context_id, revision, status, created_at, updated_at
            ) VALUES ('delegation-one', 'session-feature-status', 'sandbox-feature-status',
                      'context-feature-status', 1, 'completed', ?, ?),
                     ('delegation-two', 'session-feature-status', 'sandbox-feature-status',
                      'context-feature-status', 2, 'running', ?, ?)
            """,
            (now, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO delegation_reviews(
                id, delegation_id, revision, status, result_json, source_merged_at,
                created_at, updated_at
            ) VALUES ('review-one', 'delegation-two', 1, 'completed', '{"approved": false}',
                      NULL, ?, ?),
                     ('review-two', 'delegation-two', 2, 'completed', '{"approved": true}',
                      '2026-08-14T01:00:00Z', ?, ?)
            """,
            (now, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO delegation_change_requests(
                id, delegation_id, revision, status, instructions, provider, model,
                task_id, created_at, updated_at
            ) VALUES ('change-one', 'delegation-two', 1, 'failed', 'Fix it', 'test', 'test',
                      NULL, ?, ?),
                     ('change-two', 'delegation-two', 2, 'awaiting_review', 'Fix it again',
                      'test', 'test', NULL, ?, ?)
            """,
            (now, now, now, now),
        )
        connection.execute(
            """
            INSERT INTO sandbox_publications(
                sandbox_id, session_id, remote_branch, pr_number, pr_state,
                pr_merged_at, updated_at
            ) VALUES ('sandbox-feature-status', 'session-feature-status',
                      'feature/status', 42, 'open', '2026-08-14T02:00:00Z', ?)
            """,
            (now,),
        )

    rows = store.planning_sessions_for_project("project-feature-status")

    assert len(rows) == 1
    assert {
        key: rows[0][key]
        for key in (
            "context_status",
            "delegation_status",
            "review_status",
            "review_result_json",
            "review_source_merged_at",
            "change_status",
            "pr_number",
            "pr_state",
            "pr_merged_at",
        )
    } == {
        "context_status": "ready",
        "delegation_status": "running",
        "review_status": "completed",
        "review_result_json": '{"approved": true}',
        "review_source_merged_at": "2026-08-14T01:00:00Z",
        "change_status": "awaiting_review",
        "pr_number": 42,
        "pr_state": "open",
        "pr_merged_at": "2026-08-14T02:00:00Z",
    }


def test_publication_merge_fact_survives_later_observations(tmp_path: Path) -> None:
    store = _store_with_sandbox(
        tmp_path,
        sandbox_id="sandbox-publication-merge",
        project_id="project-publication-merge",
    )
    session_id = "session-publication-merge"
    merged_at = "2026-08-14T00:00:00Z"
    store.create_planning_session(
        session_id=session_id,
        project_id="project-publication-merge",
        sandbox_id="sandbox-publication-merge",
        project_name="Publication merge",
        title="Plan",
        status="plan_ready",
        clarifier_provider="test",
        planner_provider="test",
        reviewer_provider="test",
        credential_profile="default",
        max_review_turns=1,
    )

    store.record_sandbox_publication(
        sandbox_id="sandbox-publication-merge",
        remote_branch="feature/publication-merge",
        last_pushed_commit="a" * 40,
        remote_branch_sha="a" * 40,
        pr_number=42,
        pr_url="https://github.com/owner/repository/pull/42",
        pr_state="closed",
        pr_merged_at=merged_at,
        last_error=None,
        session_id=session_id,
    )

    facts = store.planning_session_with_feature_facts(session_id)

    assert facts is not None
    assert facts["pr_merged_at"] == merged_at

    publication = store.record_sandbox_publication(
        sandbox_id="sandbox-publication-merge",
        remote_branch="feature/publication-merge",
        last_pushed_commit="a" * 40,
        remote_branch_sha="a" * 40,
        last_error=None,
        pr_merged_at=None,
    )

    assert publication["pr_merged_at"] == merged_at
    facts = store.planning_session_with_feature_facts(session_id)
    assert facts is not None
    assert facts["pr_merged_at"] == merged_at


def _planning_delegation_counts(
    store: ControllerStore,
    sandbox_id: str,
) -> dict[str, int]:
    with store._connection() as connection:
        return {
            "planning_sessions": connection.execute(
                "SELECT count(*) FROM planning_sessions WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()[0],
            "planning_messages": connection.execute(
                """
                SELECT count(*) FROM planning_messages WHERE session_id IN (
                    SELECT id FROM planning_sessions WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "planning_findings": connection.execute(
                """
                SELECT count(*) FROM planning_findings WHERE session_id IN (
                    SELECT id FROM planning_sessions WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "planning_plan_revisions": connection.execute(
                """
                SELECT count(*) FROM planning_plan_revisions WHERE session_id IN (
                    SELECT id FROM planning_sessions WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "implementation_contexts": connection.execute(
                "SELECT count(*) FROM implementation_contexts WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()[0],
            "delegations": connection.execute(
                "SELECT count(*) FROM delegations WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()[0],
            "delegation_reviews": connection.execute(
                """
                SELECT count(*) FROM delegation_reviews WHERE delegation_id IN (
                    SELECT id FROM delegations WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "delegation_change_requests": connection.execute(
                """
                SELECT count(*) FROM delegation_change_requests WHERE delegation_id IN (
                    SELECT id FROM delegations WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "work_items": connection.execute(
                """
                SELECT count(*) FROM work_items WHERE delegation_id IN (
                    SELECT id FROM delegations WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "work_item_runs": connection.execute(
                """
                SELECT count(*) FROM work_item_runs WHERE delegation_id IN (
                    SELECT id FROM delegations WHERE sandbox_id = ?
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "work_item_routing": connection.execute(
                """
                SELECT count(*) FROM work_item_routing WHERE work_item_id IN (
                    SELECT id FROM work_items WHERE delegation_id IN (
                        SELECT id FROM delegations WHERE sandbox_id = ?
                    )
                )
                """,
                (sandbox_id,),
            ).fetchone()[0],
            "tasks": connection.execute(
                "SELECT count(*) FROM tasks WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()[0],
        }


def test_delete_v1_sandbox_manifest_deletes_planning_and_delegation_children(
    tmp_path: Path,
) -> None:
    store = _store_with_sandbox(
        tmp_path,
        sandbox_id="v1-sandbox",
        project_id="v1-project",
    )
    _seed_planning_delegation_tree(
        store,
        sandbox_id="v1-sandbox",
        project_id="v1-project",
    )
    assert set(_planning_delegation_counts(store, "v1-sandbox").values()) == {1}

    store.delete_v1_sandbox_manifest("v1-sandbox")

    assert set(_planning_delegation_counts(store, "v1-sandbox").values()) == {0}
    assert store.sandbox("v1-sandbox") is None
    assert store.project("v1-project") is not None
    _assert_database_is_consistent(store.database_path)


def test_fresh_database_applies_sandbox_migrations(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")

    store.initialize()

    assert store.applied_versions() == [
        1,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
    ]


def test_migration_21_applies_when_22_and_23_are_already_stamped(
    tmp_path: Path,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    with store._connection() as connection:
        connection.execute("DROP TABLE sandbox_leases")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")

    store.initialize()

    assert store.applied_versions() == [
        1,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
    ]
    with store._connection() as connection:
        assert (
            connection.execute(
                """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'sandbox_leases'
            """
            ).fetchone()
            is not None
        )
    schema = _sandbox_column_schema(store.database_path)
    assert {column: schema[column] for column in SANDBOX_LIFECYCLE_COLUMNS} == (
        SANDBOX_LIFECYCLE_COLUMNS
    )
    assert _project_schema(store.database_path) == PROJECT_COLUMNS
    project_indexes = _project_index_sql(store.database_path)
    assert set(project_indexes) == {
        "sqlite_autoindex_projects_1",
        "projects_source_path",
        "projects_remote_url",
    }
    assert "WHERE source_path IS NOT NULL" in project_indexes["projects_source_path"]
    assert "WHERE remote_url IS NOT NULL" in project_indexes["projects_remote_url"]
    with sqlite3.connect(store.database_path) as connection:
        projects_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
        ).fetchone()[0]
    assert "UNIQUE" not in projects_sql.upper()

    store.register_v1_project(
        project_id="project-1",
        remote_url="https://example.test/project-1.git",
        default_branch="main",
        mirror_volume="project-1-mirror",
        created_at="2026-08-11T00:00:00+00:00",
    )
    store.register_v1_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        volume_name="sample-volume",
        created_at="2026-08-11T00:00:00+00:00",
    )

    assert store.sandboxes()[0]["pr_requested"] == 0


def test_projects_partial_unique_indexes_allow_nulls_and_reject_duplicates(
    tmp_path: Path,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()

    with store._connection() as connection:
        connection.executemany(
            "INSERT INTO projects(id, source_path, remote_url, created_at) VALUES (?, ?, ?, ?)",
            [
                ("null-source-1", None, None, "2026-08-11T00:00:00+00:00"),
                ("null-source-2", None, None, "2026-08-11T00:00:00+00:00"),
                ("source-1", "/projects/one", None, "2026-08-11T00:00:00+00:00"),
                (
                    "remote-1",
                    None,
                    "https://example.test/one",
                    "2026-08-11T00:00:00+00:00",
                ),
            ],
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO projects(id, source_path, created_at) VALUES (?, ?, ?)",
                ("source-2", "/projects/one", "2026-08-11T00:00:00+00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO projects(id, remote_url, created_at) VALUES (?, ?, ?)",
                ("remote-2", "https://example.test/one", "2026-08-11T00:00:00+00:00"),
            )


def test_initialize_applies_migrations_in_order_once_and_skips_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    calls: list[int] = []

    def migration(version: int):
        def apply(connection: sqlite3.Connection) -> None:
            calls.append(version)

        return apply

    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        {101: migration(101), 102: migration(102), 103: migration(103)},
    )
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (102, ?)",
            ("2026-08-11T00:00:00+00:00",),
        )

    store.initialize()
    store.initialize()

    assert calls == [101, 103]
    assert store.applied_versions() == [
        1,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        101,
        102,
        103,
    ]


def test_migration_starts_after_previous_stamp_in_autocommit_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    observations: list[tuple[bool, int]] = []

    def disable_foreign_keys(connection: sqlite3.Connection) -> None:
        observations.append((connection.in_transaction, -1))
        connection.execute("PRAGMA foreign_keys = OFF")
        observations[-1] = (
            observations[-1][0],
            int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
        )

    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        {18: lambda connection: None, 19: disable_foreign_keys},
    )

    store.initialize()

    assert observations == [(False, 0)]


def test_migration_can_rebuild_parent_table_with_foreign_key_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")

    def create_parent_and_child(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE migration_projects (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE migration_sandboxes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES migration_projects(id)
            )
            """
        )
        connection.execute(
            "INSERT INTO migration_projects(id, name) VALUES ('project-1', 'Before')"
        )
        connection.execute(
            """
            INSERT INTO migration_sandboxes(id, project_id)
            VALUES ('sandbox-1', 'project-1')
            """
        )

    def rebuild_parent(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        connection.execute(
            """
            CREATE TABLE migration_projects_rebuilt (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO migration_projects_rebuilt(id, name)
            SELECT id, name FROM migration_projects
            """
        )
        connection.execute("DROP TABLE migration_projects")
        connection.execute(
            "ALTER TABLE migration_projects_rebuilt RENAME TO migration_projects"
        )

    monkeypatch.setattr(
        migrations_module,
        "MIGRATIONS",
        {18: create_parent_and_child, 19: rebuild_parent},
    )

    store.initialize()

    with store._connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                """
            SELECT migration_projects.id
            FROM migration_sandboxes
            JOIN migration_projects ON migration_projects.id = migration_sandboxes.project_id
            WHERE migration_sandboxes.id = 'sandbox-1'
            """
            ).fetchone()[0]
            == "project-1"
        )


def test_failed_migration_preserves_earlier_stamps_and_retries_only_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    calls: list[int] = []
    failed_once = True

    def succeed(connection: sqlite3.Connection) -> None:
        calls.append(18)

    def fail_once(connection: sqlite3.Connection) -> None:
        nonlocal failed_once
        calls.append(19)
        if failed_once:
            failed_once = False
            raise RuntimeError("migration failed")

    monkeypatch.setattr(migrations_module, "MIGRATIONS", {18: succeed, 19: fail_once})

    with pytest.raises(RuntimeError, match="migration failed"):
        store.initialize()

    assert store.applied_versions() == [1, 18]

    store.initialize()

    assert calls == [18, 19, 19]
    assert store.applied_versions() == [1, 18, 19]


def test_add_column_is_idempotent_and_reraises_other_errors(tmp_path: Path) -> None:
    database_path = tmp_path / "columns.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
        store_module._add_column(connection, "example", "name", "TEXT")
        store_module._add_column(connection, "example", "name", "TEXT")

        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(example)")
        }
        assert columns == {"id", "name"}

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            store_module._add_column(connection, "missing", "name", "TEXT")


@pytest.mark.parametrize(
    ("initial_versions", "upgraded_versions"),
    [
        ([1], [1, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31]),
        (list(range(1, 18)), [*range(1, 32)]),
    ],
    ids=["initial-schema", "pre-squash-schema"],
)
def test_upgrade_preserves_data_and_reruns_no_applied_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_versions: list[int],
    upgraded_versions: list[int],
) -> None:
    database_path = tmp_path / "controller.sqlite3"
    _seed_database_at_versions(database_path, initial_versions)
    expected_ids = _database_ids(database_path)
    store = ControllerStore(database_path)
    assert store.applied_versions() == initial_versions

    rerun_calls: list[int] = []
    if initial_versions[-1] == 17:

        def old_migration(version: int):
            def apply(connection: sqlite3.Connection) -> None:
                rerun_calls.append(version)

            return apply

        # Stand in for the squashed 2 to 17 callables, which this database
        # already carries stamps for. Reaching one means the upgrade re-ran a
        # migration a stamp had already recorded. Only patch for this case: at
        # version 1 the same stamps are absent, so the fakes would legitimately
        # run and prove nothing.
        monkeypatch.setattr(
            migrations_module,
            "MIGRATIONS",
            {
                **{version: old_migration(version) for version in range(2, 18)},
                **migrations_module.MIGRATIONS,
            },
        )

    store.initialize()
    store.initialize()

    assert store.applied_versions() == upgraded_versions
    assert _database_ids(database_path) == expected_ids
    assert rerun_calls == []
    schema = _sandbox_column_schema(database_path)
    assert {column: schema[column] for column in SANDBOX_LIFECYCLE_COLUMNS} == (
        SANDBOX_LIFECYCLE_COLUMNS
    )
    assert _project_schema(database_path) == PROJECT_COLUMNS
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                """
            SELECT s.project_id
            FROM sandboxes AS s
            LEFT JOIN projects AS p ON p.id = s.project_id
            WHERE p.id IS NULL
            """
            ).fetchall()
            == []
        )
    _assert_database_is_consistent(database_path)


def test_existing_sandbox_is_backfilled_as_legacy(tmp_path: Path) -> None:
    store = _create_legacy_sandbox_database(tmp_path / "controller.sqlite3")

    store.initialize()

    sandbox = store.sandboxes()[0]
    assert sandbox["lifecycle_version"] == "legacy"
    assert sandbox["desired_state"] == "active"
    assert sandbox["lifecycle_status"] is None


def test_initialize_is_idempotent_and_legacy_backfill_is_guarded(
    tmp_path: Path,
) -> None:
    store = _create_legacy_sandbox_database(tmp_path / "controller.sqlite3")
    store.initialize()
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes
            SET lifecycle_version = 'v1', desired_state = 'destroyed'
            WHERE id = 'sandbox-1'
            """
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")

    store.initialize()
    store.initialize()

    sandbox = store.sandboxes()[0]
    assert sandbox["lifecycle_version"] == "v1"
    assert sandbox["desired_state"] == "destroyed"
    assert store.applied_versions() == [
        1,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
    ]


def test_projects_rebuild_keeps_sandbox_foreign_key_schema_and_data(
    tmp_path: Path,
) -> None:
    store = _create_legacy_sandbox_database(tmp_path / "controller.sqlite3")

    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        sandboxes_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sandboxes'"
        ).fetchone()[0]
        row = connection.execute(
            """
            SELECT s.project_id, p.id
            FROM sandboxes AS s
            JOIN projects AS p ON p.id = s.project_id
            WHERE s.id = 'sandbox-1'
            """
        ).fetchone()

    assert "REFERENCES projects(id)" in sandboxes_sql
    assert tuple(row) == ("project-1", "project-1")


def test_projects_rebuild_rolls_back_when_index_creation_fails(tmp_path: Path) -> None:
    database_path = tmp_path / "controller.sqlite3"
    store = _create_legacy_sandbox_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE index_name_guard (source_path TEXT)")
        connection.execute(
            "CREATE INDEX projects_source_path ON index_name_guard(source_path)"
        )

    with pytest.raises(
        sqlite3.OperationalError,
        match="index projects_source_path already exists",
    ):
        store.initialize()

    with sqlite3.connect(database_path) as connection:
        projects_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
        ).fetchone()[0]
        projects = connection.execute(
            "SELECT id, source_path FROM projects ORDER BY id"
        ).fetchall()
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]

    assert "source_path TEXT NOT NULL UNIQUE" in projects_sql
    assert projects == [("project-1", "/projects/sample")]
    assert versions == [1, 18, 19]


def test_initial_migration_creates_the_current_schema_once(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

        def columns(table: str) -> set[str]:
            return {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }

            assert {
                "baseline_commit",
                "dirty_baseline_json",
                *SANDBOX_LIFECYCLE_COLUMNS,
            } <= columns("sandboxes")

        assert {"kind", "task_id", "commit_sha"} <= columns("preview_runs")
        assert {"base_branch", "baseline_dirty_json"} <= columns("tasks")
        assert "model" in columns("planning_messages")
        assert "revision" not in columns("implementation_contexts")
        assert {
            "base_branch",
            "base_commit",
            "head_commit",
            "source_merged_at",
        } <= columns("delegation_reviews")
        assert {"routing_source", "turn_finished_at"} <= columns("work_item_runs")
        assert "prompt" in columns("delegation_change_requests")
        assert {
            "sandbox_id",
            "operation",
            "operation_id",
            "owner",
            "acquired_at",
            "heartbeat_at",
        } == columns("sandbox_leases")
        assert {
            "sandbox_id",
            "signals_json",
            "proposed_engine",
            "confirmed_engine",
            "migrate_commands_json",
            "seed_commands_json",
            "commands_source",
            "detected_at_commit",
            "actor",
            "confirmed_at",
        } == columns("sandbox_engine_detections")
        assert {
            "sandbox_id",
            "engine",
            "db_name",
            "username",
            "password",
            "status",
            "provisioned_at",
            "updated_at",
        } == columns("sandbox_databases")
        assert {
            "sandbox_id",
            "remote_branch",
            "last_pushed_commit",
            "remote_branch_sha",
            "pr_number",
            "pr_url",
            "pr_state",
            "pr_merged_at",
            "last_error",
            "updated_at",
            "session_id",
        } == columns("sandbox_publications")

    assert versions == [1, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31]
    assert {
        "tasks",
        "planning_sessions",
        "implementation_contexts",
        "delegations",
        "delegation_reviews",
        "delegation_change_requests",
        "work_items",
        "work_item_runs",
        "agent_writer_sessions",
        "project_mirror_locks",
        "sandbox_tombstones",
        "sandbox_engine_detections",
        "sandbox_databases",
        "sandbox_publications",
        "sandbox_resources",
    } <= tables
    assert "one_context_per_session" in indexes
