import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.controller.store import ControllerStore, get_controller_store
from app.main import app


def _item(key: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "title": f"Item {key}",
        "objective": "do the thing",
        "scope": "just the thing",
        "dependencies": [],
        "acceptance_criteria": ["done"],
        "verification": [{"command_kind": "build"}],
        "complexity": "low",
    }
    item.update(overrides)
    return item


@pytest.fixture
def store(tmp_path: Path) -> ControllerStore:
    controller_store = ControllerStore(tmp_path / "controller.sqlite3")
    controller_store.initialize()
    controller_store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample",
        source_path="/projects/sample",
        volume_name="sample-volume",
        status="ready",
        created_at="2026-08-08T00:00:00Z",
    )
    controller_store.create_planning_session(
        session_id="session-1",
        project_id="project-1",
        sandbox_id="sandbox-1",
        project_name="sample",
        title="Do it",
        status="plan_ready",
        clarifier_provider="claude",
        planner_provider="claude",
        reviewer_provider="codex",
        credential_profile="default",
        max_review_turns=3,
    )
    controller_store.set_plan_spec(
        session_id="session-1",
        plan_spec={"scope": "Do it", "approach": "Use the current structure"},
    )
    controller_store.start_implementation_context(
        {
            "id": "context-1",
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "status": "generating",
            "provider": "claude",
            "model": "model",
        }
    )
    controller_store.settle_implementation_context(
        "context-1",
        to_status="ready",
        changes={
            "manifest_json": json.dumps({"patterns": ["follow local patterns"]}),
            "commands_json": json.dumps(
                [
                    {
                        "kind": "build",
                        "command": "npm run build",
                        "confirmed": True,
                        "reason": "defined",
                    }
                ]
            ),
        },
    )
    return controller_store


def _client(store: ControllerStore) -> tuple[TestClient, str]:
    app.dependency_overrides[get_controller_store] = lambda: store
    return (
        TestClient(app),
        "/projects/sample/planning/sessions/session-1/delegations",
    )


def test_decomposition_can_be_submitted_and_read_back(
    store: ControllerStore,
) -> None:
    client, path = _client(store)
    try:
        response = client.post(
            path,
            json={"items": [_item("a"), _item("b", dependencies=["a"])]},
        )
        body = response.json()
        read = client.get(f"{path}/{body['delegation']['id']}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201, response.text
    assert body["waves"] == [["a"], ["b"]]
    assert body["ready"] == ["a"]
    assert body["items"][1]["blocked_by"] == ["a"]
    assert read.status_code == 200
    assert read.json()["delegation"]["revision"] == 1


def test_invalid_graph_returns_unprocessable_entity(store: ControllerStore) -> None:
    client, path = _client(store)
    try:
        response = client.post(
            path,
            json={"items": [_item("a", dependencies=["ghost"])]},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "ghost" in response.json()["detail"]


def test_engine_confirmation_blocks_delegation_with_an_operator_action(
    store: ControllerStore,
) -> None:
    with store._connection() as connection:
        connection.execute(
            """
            UPDATE sandboxes
            SET lifecycle_version = 'v1', desired_state = 'active',
                lifecycle_status = 'awaiting_engine_confirmation'
            WHERE id = 'sandbox-1'
            """
        )
    client, path = _client(store)
    try:
        response = client.post(path, json={"items": [_item("a")]})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert "confirm the database engine" in response.json()["detail"]


def test_lifecycle_routes_move_delegation(store: ControllerStore) -> None:
    client, path = _client(store)
    try:
        created = client.post(path, json={"items": [_item("a")]}).json()
        delegation_path = f"{path}/{created['delegation']['id']}"
        started = client.post(f"{delegation_path}/start")
        halted = client.post(
            f"{delegation_path}/halt",
            json={"reason": "look at this"},
        )
        abandoned = client.post(f"{delegation_path}/abandon")
    finally:
        app.dependency_overrides.clear()

    assert started.json()["delegation"]["status"] == "running"
    assert halted.json()["delegation"]["status"] == "halted"
    assert halted.json()["delegation"]["error"] == "look at this"
    assert abandoned.json()["delegation"]["status"] == "abandoned"
    assert abandoned.json()["delegation"]["settled_at"] is not None


def test_listing_returns_newest_revision_first(store: ControllerStore) -> None:
    client, path = _client(store)
    try:
        first = client.post(path, json={"items": [_item("a")]}).json()
        client.post(f"{path}/{first['delegation']['id']}/abandon")
        client.post(path, json={"items": [_item("a")]})
        listed = client.get(path)
    finally:
        app.dependency_overrides.clear()

    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert [item["revision"] for item in listed.json()["delegations"]] == [2, 1]


def test_routes_enforce_project_and_session_scope(store: ControllerStore) -> None:
    client, path = _client(store)
    try:
        created = client.post(path, json={"items": [_item("a")]}).json()
        delegation_id = created["delegation"]["id"]
        wrong_project = client.get(
            "/projects/other/planning/sessions/session-1/delegations"
        )
        wrong_session = client.get(
            f"/projects/sample/planning/sessions/other/delegations/{delegation_id}"
        )
    finally:
        app.dependency_overrides.clear()

    assert wrong_project.status_code == 404
    assert wrong_session.status_code == 404


def test_packet_route_returns_the_inspectable_run_input(
    store: ControllerStore,
) -> None:
    client, path = _client(store)
    try:
        created = client.post(path, json={"items": [_item("a")]}).json()
        delegation_id = created["delegation"]["id"]
        response = client.get(f"{path}/{delegation_id}/items/a/packet")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["work_item_key"] == "a"
    assert response.json()["verification"][0]["command"] == "npm run build"


def test_execution_routes_are_project_session_and_delegation_scoped() -> None:
    paths = app.openapi()["paths"]
    prefix = "/projects/{project_name}/planning/sessions/{session_id}/delegations"

    assert f"{prefix}/{{delegation_id}}/items/{{key}}/run" in paths
    assert f"{prefix}/{{delegation_id}}/runs/{{run_id}}/accept" in paths
    assert f"{prefix}/{{delegation_id}}/runs/{{run_id}}/reject" in paths
    assert f"{prefix}/{{delegation_id}}/items/{{key}}/routing" in paths
    assert f"{prefix}/{{delegation_id}}/review" in paths
    assert f"{prefix}/{{delegation_id}}/changes" in paths
    assert f"{prefix}/{{delegation_id}}/diff" in paths
    assert f"{prefix}/{{delegation_id}}/merge" in paths
