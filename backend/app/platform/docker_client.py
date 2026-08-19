from collections.abc import Iterator

import docker
from docker.client import DockerClient
from docker.errors import DockerException
from fastapi import HTTPException, status


def get_docker_client() -> Iterator[DockerClient]:
    try:
        client = docker.from_env()
    except DockerException as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Docker daemon is unavailable",
        ) from error

    try:
        yield client
    finally:
        client.close()
