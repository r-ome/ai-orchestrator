"""Run controller-confirmed verification commands inside the sandbox volume."""

import shlex
from dataclasses import dataclass
from typing import Any

from docker.errors import DockerException

from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened
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

    try:
        environment = {"HOME": "/tmp/home", "TERM": "dumb"}
        volumes = {volume_name: {"bind": "/workspace", "mode": "rw"}}
        if database_runtime is not None:
            environment.update(database_runtime.environment)
            volumes.update(database_runtime.volumes)
        result = run_hardened(
            docker_client,
            HardenedRunSpec(
                image=settings.image,
                command=command,
                entrypoint=[],
                mem_limit=settings.memory,
                working_dir="/workspace",
                environment=environment,
                labels={
                    "orchestrator.managed": "true",
                    "orchestrator.kind": "delegation-verification",
                },
                volumes=volumes,
                timeout_seconds=settings.timeout_seconds,
                max_log_bytes=settings.max_output_bytes,
                egress=Egress.DENIED,
                network=(
                    database_runtime.network_name
                    if database_runtime is not None and database_runtime.engine != "sqlite"
                    else None
                ),
                capture=Capture.COMBINED,
            ),
        )
    except DockerException as error:
        raise VerificationOperationError(
            503,
            f"Could not run verification command '{entry.command}': {error}",
        ) from error

    if result.timed_out:
        detail = f"Timed out after {settings.timeout_seconds} seconds"
    elif result.exit_code == 0:
        detail = "Passed"
    elif result.exit_code is None:
        detail = "Container returned no exit status"
    else:
        detail = f"Exited with status {result.exit_code}"
    return {
        "command_kind": entry.command_kind,
        "command": entry.command,
        "reason": entry.reason,
        "passed": not result.timed_out and result.exit_code == 0,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "detail": detail,
        "output": result.stdout,
    }
