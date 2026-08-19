from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import DockerException

from app.containers.hardened import (
    AUTO_REMOVE,
    CAP_DROP,
    INIT,
    PIDS_LIMIT,
    READ_ONLY,
    SECURITY_OPT,
    Capabilities,
    Capture,
    Egress,
    HardenedContainerSpec,
    HardenedRunSpec,
    Rootfs,
    create_hardened,
    run_hardened,
)

# Each later migration deletes a line. An empty set means the migration is complete.
_NOT_YET_MIGRATED: set[str] = set()


class _Container:
    def __init__(
        self,
        *,
        status: Any = {"StatusCode": 0},
        logs: dict[tuple[bool, bool], bytes] | None = None,
        wait_error: Exception | None = None,
        logs_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.logs_by_stream = logs or {}
        self.wait_error = wait_error
        self.logs_error = logs_error
        self.started = False
        self.killed = False
        self.removed = False
        self.log_calls: list[tuple[bool, bool]] = []

    def start(self) -> None:
        self.started = True

    def wait(self, *, timeout: int) -> Any:
        if self.wait_error is not None:
            raise self.wait_error
        return self.status

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        self.log_calls.append((stdout, stderr))
        if self.logs_error is not None:
            raise self.logs_error
        return self.logs_by_stream.get((stdout, stderr), b"")

    def kill(self) -> None:
        self.killed = True

    def remove(self, *, force: bool) -> None:
        assert force is True
        self.removed = True


class _Containers:
    def __init__(self, container: _Container) -> None:
        self.container = container
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Container:
        self.calls.append(kwargs)
        return self.container


class _Networks:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.connected: list[_Container] = []

    def get(self, name: str) -> SimpleNamespace:
        self.requested.append(name)
        return SimpleNamespace(connect=self.connected.append)


def _client(container: _Container) -> tuple[Any, _Containers, _Networks]:
    containers = _Containers(container)
    networks = _Networks()
    return SimpleNamespace(containers=containers, networks=networks), containers, networks


def _spec(**overrides: Any) -> HardenedRunSpec:
    values: dict[str, Any] = {
        "image": "test-image",
        "command": ["echo", "ok"],
        "working_dir": "/workspace",
        "environment": {"TERM": "dumb"},
        "labels": {"orchestrator.kind": "test"},
        "volumes": {"workspace": {"bind": "/workspace", "mode": "ro"}},
        "mem_limit": "2g",
        "timeout_seconds": 60,
        "max_log_bytes": 100,
    }
    values.update(overrides)
    return HardenedRunSpec(**values)


def test_constants_land_on_create_for_a_minimal_spec() -> None:
    client, containers, _networks = _client(_Container())

    run_hardened(client, _spec())

    call = containers.calls[0]
    assert call["read_only"] is READ_ONLY is True
    assert call["cap_drop"] == ["ALL"] == list(CAP_DROP)
    assert call["security_opt"] == ["no-new-privileges:true"] == list(SECURITY_OPT)
    assert call["init"] is INIT is True
    assert call["auto_remove"] is AUTO_REMOVE is False
    assert call["pids_limit"] == PIDS_LIMIT == 512
    assert call["tmpfs"] == {"/tmp": "rw,nosuid,size=256m"}
    assert "entrypoint" not in call


@pytest.mark.parametrize(
    ("egress", "network", "expected_create", "connects"),
    [
        (Egress.PROVIDER, None, {}, []),
        (Egress.PROVIDER, "database", {}, ["database"]),
        (Egress.DENIED, None, {"network_mode": "none"}, []),
        (Egress.DENIED, "database", {"network": "database"}, []),
    ],
)
def test_network_construction(
    egress: Egress,
    network: str | None,
    expected_create: dict[str, str],
    connects: list[str],
) -> None:
    container = _Container()
    client, containers, networks = _client(container)

    run_hardened(
        client,
        _spec(egress=egress, network=network),
    )

    call = containers.calls[0]
    assert {key: call[key] for key in ("network", "network_mode") if key in call} == expected_create
    assert networks.requested == connects
    assert networks.connected == ([container] if connects else [])


def test_combined_and_separate_capture_cap_each_stream() -> None:
    combined = _Container(logs={(True, True): b"discardcombined"})
    combined_client, _containers, _networks = _client(combined)

    combined_result = run_hardened(
        combined_client,
        _spec(capture=Capture.COMBINED, max_log_bytes=len(b"combined")),
    )

    assert combined_result.stdout == "combined"
    assert combined_result.stderr == ""
    assert combined.log_calls == [(True, True)]

    separate = _Container(
        logs={(True, False): b"discardstdout", (False, True): b"discardstderr"},
    )
    separate_client, _containers, _networks = _client(separate)

    separate_result = run_hardened(
        separate_client,
        _spec(capture=Capture.SEPARATE, max_log_bytes=6),
    )

    assert separate_result.stdout == "stdout"
    assert separate_result.stderr == "stderr"
    assert separate.log_calls == [(True, False), (False, True)]


def test_log_error_yields_empty_stream() -> None:
    container = _Container(logs_error=DockerException("daemon unavailable"))
    client, _containers, _networks = _client(container)

    result = run_hardened(client, _spec())

    assert result.stdout == ""
    assert result.stderr == ""


def test_container_is_removed_when_wait_raises() -> None:
    container = _Container(wait_error=DockerException("wait failed"))
    client, _containers, _networks = _client(container)

    with pytest.raises(DockerException, match="wait failed"):
        run_hardened(client, _spec())

    assert container.removed is True


def test_hardening_is_defined_once() -> None:
    app_root = Path(__file__).parents[2] / "app"
    found = {
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if "security_opt" in path.read_text()
    }

    assert found - _NOT_YET_MIGRATED == {"containers/hardened.py"}


def test_the_not_yet_migrated_list_does_not_go_stale() -> None:
    """A migrated file must leave the list, or the list stops meaning anything.

    Without this, a site could move onto the adapter while its name stayed
    behind, and the remaining work would read as larger than it is.
    """
    app_root = Path(__file__).parents[2] / "app"
    already_migrated = {
        name
        for name in _NOT_YET_MIGRATED
        if "security_opt" not in (app_root / name).read_text()
    }

    assert already_migrated == set()


def _container_spec(**overrides: Any) -> HardenedContainerSpec:
    values: dict[str, Any] = {
        "image": "test-image",
        "labels": {"orchestrator.kind": "test"},
    }
    values.update(overrides)
    return HardenedContainerSpec(**values)


def test_create_hardened_applies_the_same_constants_and_returns_it_unstarted() -> None:
    container = _Container()
    client, containers, _networks = _client(container)

    created = create_hardened(client, _container_spec())

    call = containers.calls[0]
    assert created is container
    assert container.started is False
    assert call["read_only"] is True
    assert call["cap_drop"] == ["ALL"]
    assert call["security_opt"] == ["no-new-privileges:true"]
    assert call["init"] is True
    assert call["pids_limit"] == PIDS_LIMIT
    assert "cap_add" not in call


def test_a_created_container_joins_its_named_network_at_creation() -> None:
    client, containers, networks = _client(_Container())

    create_hardened(client, _container_spec(egress=Egress.PROVIDER, network="preview"))

    assert containers.calls[0]["network"] == "preview"
    assert networks.requested == []


def test_a_provider_run_joins_the_bridge_then_connects_its_network() -> None:
    """A run needs egress, so it cannot be created on the internal network."""
    container = _Container()
    client, containers, networks = _client(container)

    run_hardened(client, _spec(egress=Egress.PROVIDER, network="database"))

    call = containers.calls[0]
    assert "network" not in call and "network_mode" not in call
    assert networks.connected == [container]


def test_a_writable_rootfs_keeps_every_other_constant() -> None:
    client, containers, _networks = _client(_Container())

    create_hardened(client, _container_spec(rootfs=Rootfs.WRITABLE))

    call = containers.calls[0]
    assert call["read_only"] is False
    assert call["cap_drop"] == ["ALL"]
    assert call["security_opt"] == ["no-new-privileges:true"]
    assert call["pids_limit"] == PIDS_LIMIT


def test_database_capabilities_are_the_fixed_documented_set() -> None:
    client, containers, _networks = _client(_Container())

    create_hardened(client, _container_spec(capabilities=Capabilities.DATABASE_SERVER))

    assert containers.calls[0]["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"]
    assert containers.calls[0]["cap_drop"] == ["ALL"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(256, 256), (PIDS_LIMIT, PIDS_LIMIT), (PIDS_LIMIT + 1, PIDS_LIMIT), (10_000, PIDS_LIMIT)],
)
def test_a_call_site_may_tighten_the_pids_limit_but_never_raise_it(
    requested: int,
    expected: int,
) -> None:
    client, containers, _networks = _client(_Container())

    create_hardened(client, _container_spec(pids_limit=requested))

    assert containers.calls[0]["pids_limit"] == expected


def test_extra_tmpfs_mounts_join_the_standard_tmp_mount() -> None:
    client, containers, _networks = _client(_Container())

    create_hardened(
        client,
        _container_spec(tmpfs_size="32m", extra_tmpfs={"/git": "rw,nosuid,size=1m"}),
    )

    assert containers.calls[0]["tmpfs"] == {
        "/tmp": "rw,nosuid,size=32m",
        "/git": "rw,nosuid,size=1m",
    }


def test_unset_optional_arguments_are_not_passed_to_docker() -> None:
    """Docker's own default must stand where the caller named nothing."""
    client, containers, _networks = _client(_Container())

    create_hardened(client, _container_spec())

    call = containers.calls[0]
    for absent in ("name", "mounts", "ports", "restart_policy", "healthcheck", "nano_cpus",
                   "command", "entrypoint", "working_dir", "user", "mem_limit"):
        assert absent not in call


@pytest.mark.parametrize("field", ["timeout_seconds", "max_log_bytes"])
def test_a_run_spec_rejects_an_unbounded_run(field: str) -> None:
    with pytest.raises(ValueError):
        _spec(**{field: 0})


def test_a_writable_container_may_keep_its_own_tmp() -> None:
    """The prepare step unpacks packages into /tmp and needs the disk, not RAM."""
    client, containers, _networks = _client(_Container())

    create_hardened(client, _container_spec(rootfs=Rootfs.WRITABLE, tmpfs_size=None))

    assert "tmpfs" not in containers.calls[0]


def test_extra_tmpfs_still_mounts_when_tmp_is_left_alone() -> None:
    client, containers, _networks = _client(_Container())

    create_hardened(
        client,
        _container_spec(
            rootfs=Rootfs.WRITABLE,
            tmpfs_size=None,
            extra_tmpfs={"/git": "rw,nosuid,size=1m"},
        ),
    )

    assert containers.calls[0]["tmpfs"] == {"/git": "rw,nosuid,size=1m"}


def test_a_read_only_container_cannot_give_up_its_scratch_space() -> None:
    with pytest.raises(ValueError, match="scratch space"):
        _container_spec(tmpfs_size=None)
