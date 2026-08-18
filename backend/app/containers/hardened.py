"""Run containers at the ADR-0006 hardened container boundary.

Hardened run: one execution of one command in one short-lived container under the
ADR-0006 boundary. A Turn is a Hardened run that invokes a model.

Two entry points share one set of boundary constants:

* `run_hardened` owns the whole lifecycle of a short-lived container. It creates,
  starts, waits, collects logs, and removes. Use it whenever the caller wants an
  exit code and output rather than a container.
* `create_hardened` returns a created, unstarted container under the same
  boundary. Use it for containers that outlive the call that made them —
  database servers, preview applications, idle agent containers — where the
  caller owns starting, network wiring, health, and teardown.

Every weakening of the boundary is a named value on the spec, never a bare
keyword argument at a call site. `Egress.PROVIDER`, `Rootfs.WRITABLE`, and
`Capabilities.DATABASE_SERVER` each name one exception and say who may use it.
Grepping for the name finds every site that takes it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    """Whether the container may reach the public internet.

    DENIED is the boundary. PROVIDER is the named exception: a turn that calls a
    model API cannot work without egress.
    """

    DENIED = "denied"
    PROVIDER = "provider"


class Capture(Enum):
    COMBINED = "combined"
    SEPARATE = "separate"


class Rootfs(Enum):
    """Whether the container's own filesystem is writable.

    READ_ONLY is the boundary. WRITABLE is the named exception, and only three
    sites may take it, all in `previews/service.py`:

    * the prepare step, because package managers write to caches and to
      `/usr/local` outside any mounted volume;
    * the preview application container, because it is built from the user's own
      Dockerfile and runs arbitrary user code that may write to its own tree;
    * a compose service, because the compose file decides this per service.

    Everything else keeps the read-only rootfs. A writable rootfs still keeps
    every other constant: dropped capabilities, no new privileges, and the
    pids limit all still apply.
    """

    READ_ONLY = "read_only"
    WRITABLE = "writable"


class Capabilities(Enum):
    """Linux capabilities added back after `cap_drop=ALL`.

    NONE is the boundary. DATABASE_SERVER is the named exception: the official
    MySQL and PostgreSQL images run an entrypoint that chowns the data directory
    and drops to an unprivileged user, which needs exactly these four. The set is
    fixed here rather than passed in, so a call site can choose the documented
    exception but cannot compose a new one.
    """

    NONE = ()
    DATABASE_SERVER = ("CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID")


@dataclass(frozen=True)
class _BoundarySpec:
    """Fields both entry points share. Not constructed directly."""

    image: str
    command: list[str] | None = None
    entrypoint: list[str] | None = None
    working_dir: str | None = None
    user: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    volumes: Mapping[str, Any] = field(default_factory=dict)
    mem_limit: str | None = None
    #: A call site may tighten this. `_pids_limit` clamps it to PIDS_LIMIT, so
    #: naming a larger number here does not raise the ceiling.
    pids_limit: int = PIDS_LIMIT
    #: `None` leaves `/tmp` on the container's own filesystem. Only a writable
    #: rootfs may ask for that; a read-only one would have no scratch space at
    #: all. The prepare step needs it, because a package manager unpacks into
    #: /tmp and a small RAM-backed mount runs out of space where a disk one
    #: does not.
    tmpfs_size: str | None = "256m"
    #: Extra tmpfs mounts beyond `/tmp`, as path -> mount options. An image that
    #: declares `VOLUME /x` needs one, or Docker creates an anonymous volume per
    #: run that `--rm` does not always reap.
    extra_tmpfs: Mapping[str, str] = field(default_factory=dict)
    egress: Egress = Egress.DENIED
    #: The Docker network to join. With `Egress.DENIED` this is an internal
    #: network, which denies egress just as `network_mode="none"` does.
    network: str | None = None
    rootfs: Rootfs = Rootfs.READ_ONLY
    capabilities: Capabilities = Capabilities.NONE

    def __post_init__(self) -> None:
        if self.tmpfs_size is None and self.rootfs is Rootfs.READ_ONLY:
            raise ValueError(
                "A read-only container needs a /tmp tmpfs to have any scratch space"
            )


@dataclass(frozen=True)
class HardenedRunSpec(_BoundarySpec):
    """One short-lived container, run to completion by `run_hardened`."""

    timeout_seconds: int = 0
    max_log_bytes: int = 0
    capture: Capture = Capture.COMBINED

    def __post_init__(self) -> None:
        # Both carry a default only because the shared base fields have
        # defaults. Neither has a sane zero: a run must be bounded in time, and
        # a run nobody can read the output of is not worth making.
        if self.timeout_seconds <= 0:
            raise ValueError("A hardened run needs a positive timeout_seconds")
        if self.max_log_bytes <= 0:
            raise ValueError("A hardened run needs a positive max_log_bytes")


@dataclass(frozen=True)
class HardenedContainerSpec(_BoundarySpec):
    """One container that outlives the call, created by `create_hardened`."""

    name: str | None = None
    mounts: list[Any] | None = None
    ports: Mapping[str, Any] | None = None
    restart_policy: Mapping[str, Any] | None = None
    healthcheck: Mapping[str, Any] | None = None
    nano_cpus: int | None = None
    #: Whether Docker reaps the container when it exits. AUTO_REMOVE is the
    #: constant for a run, because logs are collected after the wait. A
    #: long-lived container that nobody reads logs from may set this.
    auto_remove: bool = AUTO_REMOVE


@dataclass(frozen=True)
class HardenedRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


def create_hardened(docker_client: Any, spec: HardenedContainerSpec) -> Any:
    """Create a container at the boundary and return it, unstarted.

    The caller owns everything after creation: starting, connecting further
    networks, waiting for health, and removal. Docker errors keep their original
    meaning, so a caller can still map `ImageNotFound` onto its own error.
    """
    arguments = _boundary_arguments(spec, attach_network=True)
    arguments["auto_remove"] = spec.auto_remove
    _set_if(arguments, "name", spec.name)
    _set_if(arguments, "mounts", spec.mounts)
    _set_if(arguments, "ports", spec.ports)
    _set_if(arguments, "restart_policy", _plain(spec.restart_policy))
    _set_if(arguments, "healthcheck", _plain(spec.healthcheck))
    _set_if(arguments, "nano_cpus", spec.nano_cpus)
    return docker_client.containers.create(**arguments)


def run_hardened(docker_client: Any, spec: HardenedRunSpec) -> HardenedRunResult:
    """Run spec to completion and own the container lifecycle.

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
        arguments = _boundary_arguments(spec, attach_network=False)
        arguments["auto_remove"] = AUTO_REMOVE
        container = docker_client.containers.create(**arguments)
        if spec.egress is Egress.PROVIDER and spec.network is not None:
            docker_client.networks.get(spec.network).connect(container)
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


def _boundary_arguments(spec: _BoundarySpec, *, attach_network: bool) -> dict[str, Any]:
    """Build the create arguments every hardened container shares."""
    arguments: dict[str, Any] = {
        "image": spec.image,
        "init": INIT,
        "read_only": spec.rootfs is Rootfs.READ_ONLY and READ_ONLY,
        "cap_drop": list(CAP_DROP),
        "security_opt": list(SECURITY_OPT),
        "pids_limit": min(spec.pids_limit, PIDS_LIMIT),
        "environment": dict(spec.environment),
        "labels": dict(spec.labels),
        "volumes": dict(spec.volumes),
    }
    tmpfs = dict(spec.extra_tmpfs)
    if spec.tmpfs_size is not None:
        tmpfs["/tmp"] = f"rw,nosuid,size={spec.tmpfs_size}"
    if tmpfs:
        arguments["tmpfs"] = tmpfs
    if spec.capabilities is not Capabilities.NONE:
        arguments["cap_add"] = list(spec.capabilities.value)
    _set_if(arguments, "command", spec.command)
    _set_if(arguments, "mem_limit", spec.mem_limit)
    _set_if(arguments, "working_dir", spec.working_dir)
    _set_if(arguments, "user", spec.user)
    if spec.entrypoint is not None:
        arguments["entrypoint"] = spec.entrypoint
    arguments.update(_network_arguments(spec, attach_network=attach_network))
    return arguments


def _network_arguments(spec: _BoundarySpec, *, attach_network: bool) -> dict[str, Any]:
    """Decide how the container reaches, or fails to reach, the network.

    `network_mode="none"` rather than `network_disabled=True`, so localhost still
    resolves. A run that reaches a database replaces this with that internal
    network. Neither has egress. With `Egress.PROVIDER` and no named network the
    container joins Docker's default bridge.

    `attach_network` splits the two entry points. A created container joins its
    named network at creation. A run with `Egress.PROVIDER` cannot: it needs the
    default bridge to reach the provider, so `run_hardened` creates it there and
    connects the named network afterwards, leaving it on both.
    """
    if spec.network is None:
        return {} if spec.egress is Egress.PROVIDER else {"network_mode": "none"}
    if spec.egress is Egress.DENIED or attach_network:
        return {"network": spec.network}
    return {}


def _set_if(arguments: dict[str, Any], key: str, value: Any) -> None:
    """Pass a keyword only when the caller set it, so Docker's default stands."""
    if value is not None:
        arguments[key] = value


def _plain(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if value is None else dict(value)


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
