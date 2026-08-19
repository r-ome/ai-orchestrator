import io
import tarfile
from collections.abc import Iterator
from typing import Any

from docker.errors import NotFound
from fastapi.testclient import TestClient

from app.main import app
from app.platform.docker_client import get_docker_client

client = TestClient(app)


def _file_archive(content: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("config.txt")
        member.size = len(content)
        member.mode = 0o640
        archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


class StubContainer:
    id = "abc123def456789"
    short_id = "abc123def456"
    name = "example-api"
    status = "running"
    attrs = {
        "Config": {
            "Image": "example-api:latest",
            "Labels": {"project": "example"},
        },
        "Created": "2026-08-03T01:00:00Z",
        "Image": "sha256:image123",
        "State": {
            "StartedAt": "2026-08-03T01:01:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
        },
        "RestartCount": 2,
        "Platform": "linux",
        "Mounts": [
            {
                "Type": "volume",
                "Name": "example-data",
                "Source": "/var/lib/docker/volumes/example-data/_data",
                "Destination": "/data",
                "Driver": "local",
                "Mode": "rw",
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Ports": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}],
            },
            "Networks": {
                "example-network": {
                    "NetworkID": "network123",
                    "EndpointID": "endpoint123",
                    "Gateway": "172.20.0.1",
                    "IPAddress": "172.20.0.2",
                    "MacAddress": "02:42:ac:14:00:02",
                }
            },
        },
    }

    def __init__(self) -> None:
        self.stop_timeout: int | None = None
        self.remove_options: dict[str, bool] | None = None

    def stop(self, *, timeout: int) -> None:
        self.stop_timeout = timeout

    def remove(self, **kwargs: bool) -> None:
        self.remove_options = kwargs

    def get_archive(self, path: str) -> tuple[Iterator[bytes], dict[str, Any]]:
        assert path == "/app/config.txt"
        content = b"hello from the container"
        file_stat = {
            "name": "config.txt",
            "size": len(content),
            "mode": 0o640,
            "mtime": "2026-08-03T01:05:00Z",
            "linkTarget": "",
        }
        return iter([_file_archive(content)]), file_stat


class StubContainerCollection:
    def __init__(self, container: StubContainer) -> None:
        self.container = container
        self.pruned = False

    def list(self, **kwargs: Any) -> list[StubContainer]:
        assert kwargs == {"all": True}
        return [self.container]

    def get(self, container_id: str) -> StubContainer:
        if container_id not in {
            self.container.id,
            self.container.short_id,
            self.container.name,
        }:
            raise NotFound("container not found")
        return self.container

    def prune(self) -> dict[str, Any]:
        self.pruned = True
        return {
            "ContainersDeleted": ["stopped123"],
            "SpaceReclaimed": 4_096,
        }


class StubDockerClient:
    def __init__(self) -> None:
        self.container = StubContainer()
        self.containers = StubContainerCollection(self.container)


def _override(client_instance: StubDockerClient) -> Any:
    def override_docker_client() -> Iterator[StubDockerClient]:
        yield client_instance

    return override_docker_client


def test_list_and_inspect_all_containers() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        list_response = client.get("/containers/all")
        inspect_response = client.get("/containers/abc123def456")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert list_response.json() == {
        "count": 1,
        "containers": [
            {
                "id": "abc123def456",
                "name": "example-api",
                "image": "example-api:latest",
                "status": "running",
                "created": "2026-08-03T01:00:00Z",
                "ports": [
                    {
                        "container_port": 8000,
                        "protocol": "tcp",
                        "host_ip": "127.0.0.1",
                        "host_port": 8000,
                    }
                ],
            }
        ],
    }
    assert inspect_response.status_code == 200
    assert inspect_response.json() == {
        "id": "abc123def456789",
        "short_id": "abc123def456",
        "name": "example-api",
        "image": "example-api:latest",
        "image_id": "sha256:image123",
        "status": "running",
        "created": "2026-08-03T01:00:00Z",
        "started_at": "2026-08-03T01:01:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "restart_count": 2,
        "platform": "linux",
        "ports": [
            {
                "container_port": 8000,
                "protocol": "tcp",
                "host_ip": "127.0.0.1",
                "host_port": 8000,
            }
        ],
        "mounts": [
            {
                "type": "volume",
                "name": "example-data",
                "source": "/var/lib/docker/volumes/example-data/_data",
                "destination": "/data",
                "driver": "local",
                "mode": "rw",
                "read_write": True,
            }
        ],
        "networks": [
            {
                "name": "example-network",
                "network_id": "network123",
                "endpoint_id": "endpoint123",
                "gateway": "172.20.0.1",
                "ip_address": "172.20.0.2",
                "mac_address": "02:42:ac:14:00:02",
            }
        ],
        "labels": {"project": "example"},
    }


def test_read_file_from_container() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get(
            "/containers/abc123def456/files",
            params={"path": "/app/config.txt"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "container_id": "abc123def456",
        "container_name": "example-api",
        "file": {
            "path": "/app/config.txt",
            "name": "config.txt",
            "size_bytes": 24,
            "mode": "0o640",
            "modified_at": "2026-08-03T01:05:00Z",
            "link_target": "",
            "encoding": "utf-8",
            "content": "hello from the container",
        },
    }


def test_read_file_requires_safe_absolute_path() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        relative = client.get(
            "/containers/abc123def456/files",
            params={"path": "app/config.txt"},
        )
        parent = client.get(
            "/containers/abc123def456/files",
            params={"path": "/app/../secret.txt"},
        )
    finally:
        app.dependency_overrides.clear()

    expected = {"detail": "File path must be an absolute container file path"}
    assert relative.status_code == 400
    assert relative.json() == expected
    assert parent.status_code == 400
    assert parent.json() == expected


def test_remove_container_requires_confirmation() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        rejected = client.delete("/containers/abc123def456")
        assert docker_client.container.remove_options is None
        removed = client.delete(
            "/containers/abc123def456",
            params={
                "confirm": "true",
                "force": "true",
                "remove_volumes": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert rejected.status_code == 400
    assert docker_client.container.remove_options == {"force": True, "v": True}
    assert removed.status_code == 200
    assert removed.json() == {
        "id": "abc123def456",
        "name": "example-api",
        "removed": True,
        "removed_anonymous_volumes": True,
    }


def test_prune_containers_requires_confirmation() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        rejected = client.post("/containers/prune", json={"confirm": False})
        assert docker_client.containers.pruned is False
        pruned = client.post("/containers/prune", json={"confirm": True})
    finally:
        app.dependency_overrides.clear()

    assert rejected.status_code == 400
    assert docker_client.containers.pruned is True
    assert pruned.status_code == 200
    assert pruned.json() == {
        "deleted": ["stopped123"],
        "reclaimed_bytes": 4_096,
        "reclaimed": "4.00 KiB",
    }


def test_stop_container_requires_confirmation() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        rejected = client.post(
            "/containers/abc123def456/stop",
            json={"confirm": False},
        )
        assert docker_client.container.stop_timeout is None
        stopped = client.post(
            "/containers/abc123def456/stop",
            json={"confirm": True, "timeout_seconds": 20},
        )
    finally:
        app.dependency_overrides.clear()

    assert rejected.status_code == 400
    assert docker_client.container.stop_timeout == 20
    assert stopped.status_code == 200
    assert stopped.json() == {
        "id": "abc123def456",
        "name": "example-api",
        "status": "stopped",
    }


def test_inspect_missing_container_returns_not_found() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get("/containers/missing")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Docker resource not found"}


def test_read_file_enforces_maximum_limit() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get(
            "/containers/abc123def456/files",
            params={"path": "/app/config.txt", "max_bytes": 1_048_577},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
