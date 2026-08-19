import time

from docker.models.containers import Container

from app.previews.errors import PreviewOperationError
from app.sandboxes.database import wait_for_mysql_health


def _wait_for_mysql_health(
    container: Container,
    *,
    timeout_seconds: int,
) -> None:
    wait_for_mysql_health(
        container,
        timeout_seconds=timeout_seconds,
        error=PreviewOperationError,
    )


def _wait_for_container_health(
    container: Container,
    *,
    timeout_seconds: int,
) -> None:
    """Waits for the application container's first successful health probe.

    Unlike the database container, the application image runs with no
    `healthcheck` argument of our own — whether Docker reports a `Health`
    status at all depends on a `HEALTHCHECK` baked into the image. When the
    image carries none, `running` is the only signal Docker will ever offer,
    so that alone counts as the first successful probe.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        status = str(state.get("Status") or container.status)
        health_state = state.get("Health")
        if status in {"dead", "exited"} or (
            health_state is not None and str(health_state.get("Status")) == "unhealthy"
        ):
            logs = container.logs(stdout=True, stderr=True, tail=100)
            detail = (
                logs.decode("utf-8", errors="replace")
                if isinstance(logs, bytes)
                else str(logs)
            )[-8_192:]
            raise PreviewOperationError(
                422,
                f"Application container failed its health check: {detail}",
            )
        if status == "running" and (
            health_state is None or str(health_state.get("Status")) == "healthy"
        ):
            return
        time.sleep(0.5)
    raise PreviewOperationError(
        408,
        f"Application container health check exceeded {timeout_seconds} seconds",
    )
