from collections.abc import Iterator
from typing import Any

import pytest
from docker.errors import DockerException
from fastapi.testclient import TestClient

from app.main import app
from app.platform.docker_client import get_docker_client

client = TestClient(app)


class StubContainer:
    short_id = "abc123def456"
    name = "example-api"
    status = "running"
    attrs = {
        "Config": {"Image": "example-api:latest"},
        "Created": "2026-08-03T01:02:03Z",
        "NetworkSettings": {
            "Ports": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}],
            }
        },
    }

    def stats(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"stream": False}
        return {
            "read": "2026-08-03T01:02:04Z",
            "cpu_stats": {
                "cpu_usage": {"total_usage": 300},
                "system_cpu_usage": 2_000,
                "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 1_000,
            },
            "memory_stats": {
                "usage": 2_048,
                "limit": 4_096,
                "stats": {"inactive_file": 512},
            },
            "networks": {
                "eth0": {"rx_bytes": 100, "tx_bytes": 200},
            },
            "blkio_stats": {
                "io_service_bytes_recursive": [
                    {"op": "read", "value": 300},
                    {"op": "write", "value": 400},
                ]
            },
            "pids_stats": {"current": 3},
        }


class StubContainers:
    def list(self, **kwargs: Any) -> list[StubContainer]:
        assert kwargs == {"filters": {"status": "running"}}
        return [StubContainer()]


class StubDockerClient:
    containers = StubContainers()


def override_docker_client() -> Iterator[StubDockerClient]:
    yield StubDockerClient()


def test_get_running_containers() -> None:
    app.dependency_overrides[get_docker_client] = override_docker_client

    try:
        response = client.get("/containers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "count": 1,
        "containers": [
            {
                "id": "abc123def456",
                "name": "example-api",
                "image": "example-api:latest",
                "status": "running",
                "created": "2026-08-03T01:02:03Z",
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


def test_get_container_status() -> None:
    app.dependency_overrides[get_docker_client] = override_docker_client

    try:
        response = client.get("/containers/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "count": 1,
        "total_cpu_percent": 40.0,
        "total_memory_usage_bytes": 1_536,
        "total_memory_usage": "1.50 KiB",
        "total_network_received_bytes": 100,
        "total_network_sent_bytes": 200,
        "total_block_read_bytes": 300,
        "total_block_write_bytes": 400,
        "total_pids": 3,
        "containers": [
            {
                "id": "abc123def456",
                "name": "example-api",
                "cpu_percent": 40.0,
                "memory_usage_bytes": 1_536,
                "memory_usage": "1.50 KiB",
                "memory_limit_bytes": 4_096,
                "memory_limit": "4.00 KiB",
                "memory_percent": 37.5,
                "network_received_bytes": 100,
                "network_sent_bytes": 200,
                "block_read_bytes": 300,
                "block_write_bytes": 400,
                "pids": 3,
                "sampled_at": "2026-08-03T01:02:04Z",
            }
        ],
    }


class FailingContainers:
    def list(self, **kwargs: Any) -> list[StubContainer]:
        raise DockerException("connection failed")


class FailingDockerClient:
    containers = FailingContainers()


def override_failing_docker_client() -> Iterator[FailingDockerClient]:
    yield FailingDockerClient()


@pytest.mark.parametrize("path", ["/containers", "/containers/status"])
def test_container_api_when_docker_is_unavailable(path: str) -> None:
    app.dependency_overrides[get_docker_client] = override_failing_docker_client

    try:
        response = client.get(path)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Docker daemon is unavailable"}
