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
        member = tarfile.TarInfo("notes.txt")
        member.size = len(content)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


class StubVolume:
    name = "example-data"
    attrs = {
        "Name": "example-data",
        "Driver": "local",
        "Mountpoint": "/var/lib/docker/volumes/example-data/_data",
        "CreatedAt": "2026-08-03T01:00:00Z",
        "Scope": "local",
        "Labels": {"project": "example"},
        "Options": None,
    }

    def __init__(self) -> None:
        self.removed_force: bool | None = None

    def remove(self, *, force: bool) -> None:
        self.removed_force = force


class StubVolumeCollection:
    def __init__(self, volume: StubVolume) -> None:
        self.volume = volume
        self.pruned = False

    def list(self) -> list[StubVolume]:
        return [self.volume]

    def get(self, volume_name: str) -> StubVolume:
        if volume_name != self.volume.name:
            raise NotFound("volume not found")
        return self.volume

    def prune(self) -> dict[str, Any]:
        self.pruned = True
        return {
            "VolumesDeleted": ["unused-data"],
            "SpaceReclaimed": 2_048,
        }


class StubContainer:
    id = "abc123def456789"
    short_id = "abc123def456"
    name = "example-api"
    status = "running"
    attrs = {
        "Mounts": [
            {
                "Type": "volume",
                "Name": "example-data",
                "Destination": "/data",
                "RW": True,
            }
        ]
    }

    def __init__(self) -> None:
        self.stop_timeout: int | None = None

    def stop(self, *, timeout: int) -> None:
        self.stop_timeout = timeout

    def get_archive(self, path: str) -> tuple[Iterator[bytes], dict[str, Any]]:
        assert path == "/data/notes.txt"
        content = b"hello from the volume"
        file_stat = {
            "name": "notes.txt",
            "size": len(content),
            "mode": 0o644,
            "mtime": "2026-08-03T01:05:00Z",
            "linkTarget": "",
        }
        return iter([_file_archive(content)]), file_stat


class StubContainerCollection:
    def __init__(self, container: StubContainer) -> None:
        self.container = container

    def list(self, **kwargs: Any) -> list[StubContainer]:
        assert kwargs in (
            {"all": True},
            {"filters": {"status": "running"}},
        )
        return [self.container]

    def get(self, container_id: str) -> StubContainer:
        if container_id not in {
            self.container.id,
            self.container.short_id,
            self.container.name,
        }:
            raise NotFound("container not found")
        return self.container


class StubDockerClient:
    def __init__(self) -> None:
        self.volume = StubVolume()
        self.container = StubContainer()
        self.volumes = StubVolumeCollection(self.volume)
        self.containers = StubContainerCollection(self.container)


def _override(client_instance: StubDockerClient) -> Any:
    def override_docker_client() -> Iterator[StubDockerClient]:
        yield client_instance

    return override_docker_client


def test_list_and_inspect_managed_volumes() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        list_response = client.get("/volumes/all")
        inspect_response = client.get("/volumes/example-data")
    finally:
        app.dependency_overrides.clear()

    expected_volume = {
        "name": "example-data",
        "driver": "local",
        "mountpoint": "/var/lib/docker/volumes/example-data/_data",
        "created_at": "2026-08-03T01:00:00Z",
        "scope": "local",
        "labels": {"project": "example"},
        "options": None,
        "attachments": [
            {
                "container_id": "abc123def456",
                "container_name": "example-api",
                "container_status": "running",
                "destination": "/data",
                "read_write": True,
            }
        ],
    }
    assert list_response.status_code == 200
    assert list_response.json() == {"count": 1, "volumes": [expected_volume]}
    assert inspect_response.status_code == 200
    assert inspect_response.json() == expected_volume


def test_read_file_from_attached_container() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get(
            "/volumes/example-data/files",
            params={"path": "notes.txt", "container_id": "abc123def456"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "volume_name": "example-data",
        "container_id": "abc123def456",
        "container_name": "example-api",
        "container_path": "/data/notes.txt",
        "file": {
            "path": "notes.txt",
            "name": "notes.txt",
            "size_bytes": 21,
            "mode": "0o644",
            "modified_at": "2026-08-03T01:05:00Z",
            "link_target": "",
            "encoding": "utf-8",
            "content": "hello from the volume",
        },
    }


def test_read_file_rejects_parent_path() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get(
            "/volumes/example-data/files",
            params={"path": "../secret.txt"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {"detail": "File path must be relative to the volume"}


def test_remove_volume_requires_confirmation() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        rejected = client.delete("/volumes/example-data")
        assert docker_client.volume.removed_force is None
        removed = client.delete(
            "/volumes/example-data",
            params={"confirm": "true", "force": "true"},
        )
    finally:
        app.dependency_overrides.clear()

    assert rejected.status_code == 400
    assert docker_client.volume.removed_force is True
    assert removed.status_code == 200
    assert removed.json() == {"name": "example-data", "removed": True}


def test_prune_volumes_requires_confirmation() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        rejected = client.post("/volumes/prune", json={"confirm": False})
        assert docker_client.volumes.pruned is False
        pruned = client.post("/volumes/prune", json={"confirm": True})
    finally:
        app.dependency_overrides.clear()

    assert rejected.status_code == 400
    assert docker_client.volumes.pruned is True
    assert pruned.status_code == 200
    assert pruned.json() == {
        "deleted": ["unused-data"],
        "reclaimed_bytes": 2_048,
        "reclaimed": "2.00 KiB",
    }


def test_stop_attached_container_requires_confirmation() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        rejected = client.post(
            "/volumes/example-data/containers/abc123def456/stop",
            json={"confirm": False},
        )
        assert docker_client.container.stop_timeout is None
        stopped = client.post(
            "/volumes/example-data/containers/abc123def456/stop",
            json={"confirm": True, "timeout_seconds": 15},
        )
    finally:
        app.dependency_overrides.clear()

    assert rejected.status_code == 400
    assert docker_client.container.stop_timeout == 15
    assert stopped.status_code == 200
    assert stopped.json() == {
        "volume_name": "example-data",
        "container_id": "abc123def456",
        "container_name": "example-api",
        "status": "stopped",
    }


def test_inspect_missing_volume_returns_not_found() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get("/volumes/missing")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Docker resource not found"}


def test_read_file_enforces_maximum_limit() -> None:
    docker_client = StubDockerClient()
    app.dependency_overrides[get_docker_client] = _override(docker_client)

    try:
        response = client.get(
            "/volumes/example-data/files",
            params={"path": "notes.txt", "max_bytes": 1_048_577},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
