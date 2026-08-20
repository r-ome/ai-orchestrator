from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from docker.errors import DockerException
from fastapi.testclient import TestClient

from app.main import app
from app.platform.docker_client import get_docker_client

client = TestClient(app)


class StubContainer:
    short_id = "abc123def456"
    name = "example-api"
    attrs: ClassVar[dict[str, Any]] = {
        "Mounts": [
            {
                "Type": "volume",
                "Name": "example-data",
                "Source": "/var/lib/docker/volumes/example-data/_data",
                "Destination": "/data",
                "Driver": "local",
                "Mode": "z",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/host/config",
                "Destination": "/app/config",
                "Mode": "ro",
                "RW": False,
            },
        ]
    }


class StubContainers:
    def list(self, **kwargs: Any) -> list[StubContainer]:
        assert kwargs == {"filters": {"status": "running"}}
        return [StubContainer()]


class StubApi:
    def df(self) -> dict[str, Any]:
        return {
            "ImageUsage": {
                "TotalCount": 3,
                "ActiveCount": 1,
                "TotalSize": 10_000,
                "Reclaimable": 6_000,
            },
            "ContainerUsage": {
                "TotalCount": 2,
                "ActiveCount": 1,
                "TotalSize": 2_048,
                "Reclaimable": 1_024,
            },
            "VolumeUsage": {
                "TotalCount": 2,
                "ActiveCount": 1,
                "TotalSize": 1_024,
                "Reclaimable": 512,
            },
            "BuildCacheUsage": {
                "TotalCount": 1,
                "ActiveCount": 0,
                "TotalSize": 512,
                "Reclaimable": 512,
            },
        }


class StubDockerClient:
    containers = StubContainers()
    api = StubApi()


def override_docker_client() -> Iterator[StubDockerClient]:
    yield StubDockerClient()


def test_get_running_volumes() -> None:
    app.dependency_overrides[get_docker_client] = override_docker_client

    try:
        response = client.get("/volumes")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "count": 2,
        "volumes": [
            {
                "type": "bind",
                "name": None,
                "source": "/host/config",
                "destination": "/app/config",
                "driver": "",
                "mode": "ro",
                "read_write": False,
                "container_id": "abc123def456",
                "container_name": "example-api",
            },
            {
                "type": "volume",
                "name": "example-data",
                "source": "/var/lib/docker/volumes/example-data/_data",
                "destination": "/data",
                "driver": "local",
                "mode": "z",
                "read_write": True,
                "container_id": "abc123def456",
                "container_name": "example-api",
            },
        ],
    }


def test_get_docker_storage_status() -> None:
    app.dependency_overrides[get_docker_client] = override_docker_client

    try:
        response = client.get("/volumes/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "total_size_bytes": 13_584,
        "total_size": "13.27 KiB",
        "total_reclaimable_bytes": 8_048,
        "total_reclaimable": "7.86 KiB",
        "images": {
            "total_count": 3,
            "active_count": 1,
            "size_bytes": 10_000,
            "size": "9.77 KiB",
            "reclaimable_bytes": 6_000,
            "reclaimable": "5.86 KiB",
        },
        "containers": {
            "total_count": 2,
            "active_count": 1,
            "size_bytes": 2_048,
            "size": "2.00 KiB",
            "reclaimable_bytes": 1_024,
            "reclaimable": "1.00 KiB",
        },
        "volumes": {
            "total_count": 2,
            "active_count": 1,
            "size_bytes": 1_024,
            "size": "1.00 KiB",
            "reclaimable_bytes": 512,
            "reclaimable": "512 B",
        },
        "build_cache": {
            "total_count": 1,
            "active_count": 0,
            "size_bytes": 512,
            "size": "512 B",
            "reclaimable_bytes": 512,
            "reclaimable": "512 B",
        },
    }


class FailingContainers:
    def list(self, **kwargs: Any) -> list[StubContainer]:
        raise DockerException("connection failed")


class FailingApi:
    def df(self) -> dict[str, Any]:
        raise DockerException("connection failed")


class FailingDockerClient:
    containers = FailingContainers()
    api = FailingApi()


def override_failing_docker_client() -> Iterator[FailingDockerClient]:
    yield FailingDockerClient()


@pytest.mark.parametrize("path", ["/volumes", "/volumes/status"])
def test_volume_api_when_docker_is_unavailable(path: str) -> None:
    app.dependency_overrides[get_docker_client] = override_failing_docker_client

    try:
        response = client.get(path)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Docker daemon is unavailable"}
