import json
import sqlite3
from pathlib import Path

import pytest

from app.controller.store import ControllerStore
import app.controller.store as store_module


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-project",
        source_path="/projects/sample-project",
        volume_name="orchestrator-project-sample-project",
        status="ready",
        created_at="2026-08-06T00:00:00Z",
    )
    return store


def _create_session(store: ControllerStore, *, sandbox_id: str = "sandbox-1") -> None:
    store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id=sandbox_id,
        project_name="sample-project",
        title="Planning session",
        status="clarifying",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )


def test_initial_migration_creates_planning_tables(
    tmp_path: Path,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }

    assert {
        "planning_sessions",
        "planning_messages",
        "planning_findings",
        "planning_plan_revisions",
    } <= table_names
    assert versions == {1, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31}


def test_planning_session_model_columns_match_for_new_and_upgraded_databases(
    tmp_path: Path,
) -> None:
    columns = {
        "clarifier_model",
        "planner_model",
        "reviewer_model",
        "reviewer_reasoning_effort",
    }
    fresh = ControllerStore(tmp_path / "fresh.sqlite3")
    fresh.initialize()

    legacy_path = tmp_path / "legacy.sqlite3"
    legacy_schema = store_module.INITIAL_MIGRATION
    for column in columns:
        legacy_schema = legacy_schema.replace(f"    {column} TEXT,\n", "")
    with sqlite3.connect(legacy_path) as connection:
        connection.executescript(legacy_schema)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(version, "2026-08-14T00:00:00Z") for version in range(1, 30)],
        )
    legacy = ControllerStore(legacy_path)
    legacy.initialize()

    def session_columns(store: ControllerStore) -> set[str]:
        with sqlite3.connect(store.database_path) as connection:
            return {str(row[1]) for row in connection.execute("PRAGMA table_info(planning_sessions)")}

    assert columns <= session_columns(fresh)
    assert session_columns(fresh) == session_columns(legacy)


def test_initialize_twice_is_a_no_op(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            )
        ]

    assert versions == [1, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31]


def test_creating_session_for_unregistered_sandbox_raises_integrity_error(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        _create_session(store, sandbox_id="sandbox-not-registered")


def test_advance_planning_status_from_wrong_source_leaves_session_unchanged(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _create_session(store)

    moved = store.advance_planning_status(
        session_id="session-1",
        from_statuses=("planning",),
        to_status="under_review",
    )

    assert moved is False
    assert store.planning_session("session-1")["status"] == "clarifying"


def test_claim_planning_turn_allows_one_turn_until_released(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_session(store)

    assert store.claim_planning_turn("session-1") is True
    assert store.claim_planning_turn("session-1") is False
    store.release_planning_turn("session-1")
    assert store.claim_planning_turn("session-1") is True


def test_append_planning_message_numbers_sequences_from_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_session(store)

    assert store.append_planning_message(
        session_id="session-1", role="user", text="First message"
    ) == 1
    assert store.append_planning_message(
        session_id="session-1", role="clarifier", text="Second message"
    ) == 2
    assert [row["sequence"] for row in store.planning_messages("session-1")] == [1, 2]


def test_resolve_unseen_findings_resolves_only_earlier_findings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_session(store)
    store.upsert_planning_finding(
        session_id="session-1",
        finding_id="F1",
        severity="major",
        text="First finding",
        status="open",
        round_number=1,
    )
    store.upsert_planning_finding(
        session_id="session-1",
        finding_id="F2",
        severity="minor",
        text="Second finding",
        status="open",
        round_number=2,
    )

    assert store.resolve_unseen_findings(session_id="session-1", round_number=2) == 1
    findings = {row["finding_id"]: row for row in store.planning_findings("session-1")}
    assert findings["F1"]["status"] == "resolved"
    assert findings["F2"]["status"] == "open"


def test_set_plan_spec_round_trips_nested_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_session(store)
    plan_spec = {
        "title": "Planning session",
        "reviewer_outcome": {"approved": True, "rounds": 1},
        "components": [{"name": "store", "responsibility": "persist sessions"}],
    }

    store.set_plan_spec(session_id="session-1", plan_spec=plan_spec)

    session = store.planning_session("session-1")
    assert session is not None
    assert json.loads(str(session["plan_spec_json"])) == plan_spec
