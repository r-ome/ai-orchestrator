"""Cover the remote-project endpoints and their error mapping.

`projects/router.py` routes every branch through `docker_response`. Without
these, removing its error policy leaves the suite green.
"""

import pytest
from fastapi.testclient import TestClient

from app.controller.store import get_controller_store
from app.main import app
from app.sandboxes.naming import mirror_ownership_labels, mirror_volume
from conftest import register_ready_v1_sandbox

REMOTE = "https://github.com/owner/repo.git"


@pytest.fixture
def client(override_docker_client):
    # Bare client for the same reason the sandbox router tests use one: the
    # lifespan reconciler builds its own Docker client from the environment.
    yield TestClient(app)


def test_register_returns_201_then_200_for_the_same_remote(client: TestClient) -> None:
    first = client.post("/projects/remote", json={"remote_url": REMOTE})
    second = client.post("/projects/remote", json={"remote_url": REMOTE})

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["project_id"] == second.json()["project_id"]
    assert client.get("/projects/remote").json()["count"] == 1


def test_register_refuses_a_remote_without_a_host_and_path(client: TestClient) -> None:
    response = client.post("/projects/remote", json={"remote_url": "not a url"})

    assert response.status_code == 400
    assert client.get("/projects/remote").json()["count"] == 0


def test_get_and_delete_report_an_unknown_project_as_404(client: TestClient) -> None:
    assert client.get("/projects/remote/prj-missing").status_code == 404
    removed = client.delete("/projects/remote/prj-missing")

    assert removed.status_code == 404
    assert removed.json()["detail"] == "no project 'prj-missing'"


def test_delete_refuses_a_project_that_still_has_a_sandbox(client: TestClient) -> None:
    store = get_controller_store()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sbx-held",
        project_id="prj-held",
        project_name=REMOTE,
        volume_name="sbx-held-workspace",
        remote_url=REMOTE,
    )

    response = client.delete("/projects/remote/prj-held")

    assert response.status_code == 409
    assert "still has 1 sandbox(es)" in response.json()["detail"]
    assert client.get("/projects/remote/prj-held").status_code == 200


def test_delete_removes_only_a_mirror_volume_it_owns(
    client: TestClient, fake_docker_client
) -> None:
    client.post("/projects/remote", json={"remote_url": REMOTE})
    project_id = client.get("/projects/remote").json()["projects"][0]["project_id"]
    volume_name = mirror_volume(project_id)
    fake_docker_client.volumes.create(
        name=volume_name,
        driver="local",
        labels=mirror_ownership_labels(project_id=project_id),
    )

    response = client.delete(f"/projects/remote/{project_id}")

    assert response.status_code == 200
    assert response.json()["removed_mirror_volume"] == volume_name
    with pytest.raises(Exception):
        fake_docker_client.volumes.get(volume_name)


def test_delete_keeps_a_mirror_volume_it_does_not_own(
    client: TestClient, fake_docker_client
) -> None:
    client.post("/projects/remote", json={"remote_url": REMOTE})
    project_id = client.get("/projects/remote").json()["projects"][0]["project_id"]
    volume_name = mirror_volume(project_id)
    # A hand-made volume wearing the right name but not the ownership labels.
    fake_docker_client.volumes.create(name=volume_name, driver="local", labels={})

    response = client.delete(f"/projects/remote/{project_id}")

    assert response.status_code == 200
    assert response.json()["removed_mirror_volume"] is None
    assert fake_docker_client.volumes.get(volume_name) is not None
