"""Run controller-confirmed verification commands inside the sandbox volume."""

import shlex
import time
from dataclasses import dataclass
from typing import Any

from docker.errors import DockerException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from app.delegation.packet import ResolvedVerification


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
) -> dict[str, Any]:
    """Run each confirmed command once and stop at the first failure."""
    results: list[dict[str, Any]] = []
    for entry in commands:
        result = _run_command(
            docker_client,
            settings,
            volume_name=volume_name,
            entry=entry,
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
        container = docker_client.containers.create(
            image=settings.image,
            command=command,
            entrypoint=[],
            auto_remove=False,
            init=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            # `network_mode="none"` rather than `network_disabled=True`. Both
            # deny external connectivity, but `network_disabled` leaves the
            # container with an empty /etc/hosts and /etc/resolv.conf, so even
            # `localhost` fails to resolve. A build that resolves any hostname
            # then dies with `getaddrinfo EAI_AGAIN localhost` — and the run is
            # failed for a fault in the sandbox, not in the code under test.
            # This container is the only one that runs the project's own
            # commands, which is why it is the only one that needs the change.
            network_mode="none",
            pids_limit=settings.pids_limit,
            mem_limit=settings.memory,
            working_dir="/workspace",
            environment={"HOME": "/tmp/home", "TERM": "dumb"},
            labels={
                "orchestrator.managed": "true",
                "orchestrator.kind": "delegation-verification",
            },
            volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
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
