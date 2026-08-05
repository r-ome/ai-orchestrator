import asyncio

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_hello_world() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "hello world"
    assert response.headers["content-type"].startswith("text/plain")


def test_lifespan_reconciles_without_stopping_managed_runs(monkeypatch) -> None:
    reconcile_calls: list[object] = []
    monkeypatch.setattr(
        "app.main.reconcile_controller_state",
        lambda store: reconcile_calls.append(store) or {},
    )

    async def idle_expiry_loop(*_: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("app.main.expiry_loop", idle_expiry_loop)

    with TestClient(app) as lifespan_client:
        assert lifespan_client.get("/").status_code == 200

    assert len(reconcile_calls) == 1
