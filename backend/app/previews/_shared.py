import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from docker.client import DockerClient

from app.controller.store import ControllerStore
from app.previews.errors import PreviewOperationError
from app.projects.service import (
    ProjectOperationError,
    inspect_registered_project,
    managed_project_key,
)


def _ready_project(
    docker_client: DockerClient,
    project_name: str,
    controller_store: ControllerStore,
) -> Any:
    try:
        project = inspect_registered_project(
            docker_client,
            project_name,
            controller_store,
        )
    except ProjectOperationError as error:
        raise PreviewOperationError(error.status_code, error.detail) from error
    if not project.ready:
        raise PreviewOperationError(409, f"Project '{project_name}' is not ready")
    return project


def _project_key(project: Any) -> str:
    return managed_project_key(str(project.source_path))


def _safe_relative_path(value: str, *, field: str, allow_dot: bool = False) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise PreviewOperationError(422, f"{field} must stay inside the sandbox")
    text = path.as_posix()
    if not text or (text == "." and not allow_dot):
        raise PreviewOperationError(422, f"{field} is required")
    return text


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-._") or "data"


def _expiry(minutes: int) -> str | None:
    return None if minutes == 0 else _time_after(minutes=minutes)


def _time_after(*, seconds: int = 0, minutes: int = 0) -> str:
    value = datetime.now(UTC) + timedelta(seconds=seconds, minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
