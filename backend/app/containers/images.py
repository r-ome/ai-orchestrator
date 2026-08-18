"""Make an image present before a container needs it."""

from typing import Any

from docker.errors import ImageNotFound


def ensure_image(docker_client: Any, image: str) -> None:
    """Pull image unless it is already local.

    A failed pull raises the Docker error as it came, so each caller can map it
    onto whatever its own layer reports.
    """
    try:
        docker_client.images.get(image)
    except ImageNotFound:
        docker_client.images.pull(image)
