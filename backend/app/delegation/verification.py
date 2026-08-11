"""Run controller-confirmed verification commands inside the sandbox volume."""

import shlex
import time
from dataclasses import dataclass
from typing import Any

from docker.errors import DockerException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from app.delegation.packet import ResolvedVerification
from app.controller.store import ControllerStore
from app.sandboxes.database import (
    SandboxDatabaseError,
    SandboxDatabaseRuntime,
    sandbox_database_runtime,
)


@dataclass(frozen=True)
class VerificationSettings:
    image: str
    timeout_seconds: int
    memory: str
    pids_limit: int
    max_output_bytes: int


class VerificationOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def run_verification(
    docker_client: Any,
    settings: VerificationSettings,
    *,
    volume_name: str,
    commands: list[ResolvedVerification],
    controller_store: ControllerStore | None = None,
    sandbox_id: str = "",
) -> dict[str, Any]:
    """Run each confirmed command once and stop at the first failure."""
    try:
        database_runtime = (
            sandbox_database_runtime(
                docker_client,
                controller_store,
                sandbox_id,
            )
            if controller_store is not None and sandbox_id
            else None
        )
    except SandboxDatabaseError as error:
        raise VerificationOperationError(error.status_code, error.detail) from error
    results: list[dict[str, Any]] = []
    for entry in commands:
        result = _run_command(
            docker_client,
            settings,
            volume_name=volume_name,
            entry=entry,
            database_runtime=database_runtime,
        )
        results.append(result)
        if not result["passed"]:
            break
    return {
        "passed": bool(commands) and all(result["passed"] for result in results),
        "commands": results,
    }


def _run_command(
    docker_client: Any,
    settings: VerificationSettings,
    *,
    volume_name: str,
    entry: ResolvedVerification,
    database_runtime: SandboxDatabaseRuntime | None = None,
) -> dict[str, Any]:
    try:
        command = shlex.split(entry.command)
    except ValueError as error:
        raise VerificationOperationError(
            409,
            f"Confirmed command '{entry.command}' is not parseable",
        ) from error
    if not command:
        raise VerificationOperationError(409, "Confirmed verification command is empty")

    container = None
    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    output = ""
    try:
        environment = {"HOME": "/tmp/home", "TERM": "dumb"}
        volumes = {volume_name: {"bind": "/workspace", "mode": "rw"}}
        network_arguments: dict[str, Any] = {"network_mode": "none"}
        if database_runtime is not None:
            environment.update(database_runtime.environment)
            volumes.update(database_runtime.volumes)
            if database_runtime.engine != "sqlite":
                network_arguments = {"network": database_runtime.network_name}
        container = docker_client.containers.create(
            image=settings.image,
            command=command,
            entrypoint=[],
            auto_remove=False,
            init=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            # Legacy and SQLite verification uses `network_mode="none"` rather
            # than `network_disabled=True`, so localhost still resolves. A
            # server-backed sandbox replaces this with its internal database
            # network. Neither mode has egress.
            **network_arguments,
            pids_limit=settings.pids_limit,
            mem_limit=settings.memory,
            working_dir="/workspace",
            environment=environment,
            labels={
                "orchestrator.managed": "true",
                "orchestrator.kind": "delegation-verification",
            },
            volumes=volumes,
            tmpfs={"/tmp": "rw,nosuid,size=256m"},
        )
        container.start()
        try:
            status = container.wait(timeout=settings.timeout_seconds)
            value = status.get("StatusCode") if isinstance(status, dict) else status
            exit_code = int(value) if value is not None else None
        except (ReadTimeout, RequestsConnectionError):
            timed_out = True
            _kill(container)
        raw = container.logs(stdout=True, stderr=True)
        encoded = raw if isinstance(raw, bytes) else str(raw or "").encode()
        output = encoded[-settings.max_output_bytes :].decode(
            "utf-8",
            errors="replace",
        )
    except DockerException as error:
        raise VerificationOperationError(
            503,
            f"Could not run verification command '{entry.command}': {error}",
        ) from error
    finally:
        _remove(container)

    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        detail = f"Timed out after {settings.timeout_seconds} seconds"
    elif exit_code == 0:
        detail = "Passed"
    elif exit_code is None:
        detail = "Container returned no exit status"
    else:
        detail = f"Exited with status {exit_code}"
    return {
        "command_kind": entry.command_kind,
        "command": entry.command,
        "reason": entry.reason,
        "passed": not timed_out and exit_code == 0,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "detail": detail,
        "output": output,
    }


def _kill(container: Any) -> None:
    try:
        container.kill()
    except DockerException:
        pass


def _remove(container: Any) -> None:
    if container is None:
        return
    try:
        container.remove(force=True)
    except DockerException:
        pass
