"""Run commands at the ADR-0006 hardened container boundary.

Hardened run: one execution of one command in one short-lived container under the
ADR-0006 boundary. A Turn is a Hardened run that invokes a model.
"""

from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Any

from docker.errors import DockerException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout


#: The ADR-0006 boundary. These are constants rather than spec fields so that
#: weakening one is not expressible at a call site. The two sequences are
#: tuples, and every run copies them into a fresh list, so one caller cannot
#: reach in and empty the list every later run shares.
READ_ONLY = True
CAP_DROP = ("ALL",)
SECURITY_OPT = ("no-new-privileges:true",)
INIT = True
AUTO_REMOVE = False
PIDS_LIMIT = 512


class Egress(Enum):
    DENIED = "denied"
    PROVIDER = "provider"


class Capture(Enum):
    COMBINED = "combined"
    SEPARATE = "separate"


@dataclass(frozen=True)
class HardenedRunSpec:
    image: str
    command: list[str]
    working_dir: str
    environment: dict[str, str]
    labels: dict[str, str]
    volumes: dict
    mem_limit: str
    timeout_seconds: int
    max_log_bytes: int
    tmpfs_size: str = "256m"
    entrypoint: list[str] | None = None
    egress: Egress = Egress.DENIED
    database_network: str | None = None
    capture: Capture = Capture.COMBINED


@dataclass(frozen=True)
class HardenedRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


def run_hardened(docker_client: Any, spec: HardenedRunSpec) -> HardenedRunResult:
    """Run spec and own the container lifecycle.

    Docker errors from create, network setup, start, and wait keep their original
    meaning. Timeout cleanup and log collection never replace the result.
    """
    started_at = monotonic()
    container = None
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    timed_out = False
    try:
        create_arguments: dict[str, Any] = {
            "image": spec.image,
            "command": spec.command,
            "auto_remove": AUTO_REMOVE,
            "init": INIT,
            "read_only": READ_ONLY,
            "cap_drop": list(CAP_DROP),
            "security_opt": list(SECURITY_OPT),
            "pids_limit": PIDS_LIMIT,
            "mem_limit": spec.mem_limit,
            "working_dir": spec.working_dir,
            "environment": spec.environment,
            "labels": spec.labels,
            "volumes": spec.volumes,
            "tmpfs": {"/tmp": f"rw,nosuid,size={spec.tmpfs_size}"},
        }
        if spec.entrypoint is not None:
            create_arguments["entrypoint"] = spec.entrypoint
        if spec.egress is Egress.DENIED:
            if spec.database_network is None:
                # `network_mode="none"` rather than `network_disabled=True`, so
                # localhost still resolves. A run that reaches a database
                # replaces this with that internal network. Neither has egress.
                create_arguments["network_mode"] = "none"
            else:
                create_arguments["network"] = spec.database_network
        container = docker_client.containers.create(**create_arguments)
        if spec.egress is Egress.PROVIDER and spec.database_network is not None:
            docker_client.networks.get(spec.database_network).connect(container)
        container.start()
        try:
            status = container.wait(timeout=spec.timeout_seconds)
            exit_code = _exit_code(status)
        except (ReadTimeout, RequestsConnectionError):
            timed_out = True
            _kill(container)
        stdout, stderr = _logs(container, spec)
    finally:
        _remove(container)

    return HardenedRunResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=round((monotonic() - started_at) * 1000),
    )


def _exit_code(status: Any) -> int | None:
    value = status.get("StatusCode") if isinstance(status, dict) else status
    return int(value) if value is not None else None


def _logs(container: Any, spec: HardenedRunSpec) -> tuple[str, str]:
    if spec.capture is Capture.COMBINED:
        return _log(container, spec.max_log_bytes, stdout=True, stderr=True), ""
    return (
        _log(container, spec.max_log_bytes, stdout=True, stderr=False),
        _log(container, spec.max_log_bytes, stdout=False, stderr=True),
    )


def _log(container: Any, max_log_bytes: int, *, stdout: bool, stderr: bool) -> str:
    try:
        raw = container.logs(stdout=stdout, stderr=stderr)
    except DockerException:
        return ""
    if isinstance(raw, bytes):
        return raw[-max_log_bytes:].decode("utf-8", errors="replace")
    return str(raw).encode()[-max_log_bytes:].decode("utf-8", errors="replace")


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
