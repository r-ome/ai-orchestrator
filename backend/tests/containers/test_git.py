import os
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker
import pytest
from docker.errors import ContainerError, ImageNotFound
from requests.exceptions import ReadTimeout

from app.containers.git import (
    GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE,
    GITHUB_READ_TOKEN_PATH,
    GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE,
    GITHUB_WRITE_TOKEN_PATH,
    GitCredentialError,
    GitNetworkMode,
    GitTimeoutError,
    clone_mirror_to_workspace,
    count_mirror_staleness,
    fetch_canonical_mirror,
    provision_git_write_token,
    push_mirror_to_remote,
    push_workspace_to_mirror,
    remote_branch_sha,
    run_git,
    run_git_with_write_credentials,
)
from app.controller.config import get_controller_settings

requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


class _StaticCredentialSource:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def read_token(self) -> str | None:
        return self.token


class _StaticWriteCredentialSource:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def write_token(self) -> str | None:
        return self.token


class _SpyContainers:
    def __init__(
        self,
        *,
        output: bytes = b"git output",
        stderr: bytes = b"",
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.output = output
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    def create(self, **kwargs: Any) -> "_SpyContainer":
        self.calls.append(kwargs)
        return _SpyContainer(self.output, self.stderr, self.exit_code, self.timed_out)


class _SpyContainer:
    def __init__(
        self,
        output: bytes,
        stderr: bytes,
        exit_code: int,
        timed_out: bool = False,
    ) -> None:
        self.output = output
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    def start(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, *, timeout: int) -> dict[str, int]:
        if self.timed_out:
            raise ReadTimeout("git container exceeded its deadline")
        return {"StatusCode": self.exit_code}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        return self.output if stdout else self.stderr

    def remove(self, *, force: bool) -> None:
        pass


class _SpyImages:
    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.pull_calls: list[str] = []

    def get(self, image: str) -> None:
        self.get_calls.append(image)
        raise ImageNotFound("image not found")

    def pull(self, image: str) -> None:
        self.pull_calls.append(image)


class _SpyDockerClient:
    def __init__(self) -> None:
        self.containers = _SpyContainers()
        self.images = _SpyImages()


def test_run_git_defaults_to_the_hardened_no_network_configuration() -> None:
    docker_client = _SpyDockerClient()

    output = run_git(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={"project": {"bind": "/project", "mode": "rw"}},
        script="git status",
    )

    assert output == b"git output"
    call = docker_client.containers.calls[0]
    assert call["image"] == "alpine/git:latest"
    assert call["entrypoint"] == ["sh", "-c"]
    assert call["command"] == ["git status"]
    assert call["network_mode"] == "none"
    assert call["environment"] == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/tmp",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
    }
    assert call["volumes"] == {"project": {"bind": "/project", "mode": "rw"}}
    # `/git` is a tmpfs so alpine/git's declared VOLUME /git does not create
    # an anonymous volume on every controller git call.
    assert call["tmpfs"] == {"/tmp": "rw,nosuid,size=32m", "/git": "rw,nosuid,size=1m"}


def test_run_git_enables_network_only_with_the_explicit_mode() -> None:
    docker_client = _SpyDockerClient()

    run_git(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={},
        script="git fetch",
        network=GitNetworkMode.ENABLED,
    )

    call = docker_client.containers.calls[0]
    assert "network_mode" not in call
    assert "network" not in call


def test_run_git_preserves_the_container_error_contract() -> None:
    docker_client = _SpyDockerClient()
    docker_client.containers = _SpyContainers(
        stderr=b"fatal: no remote\n", exit_code=128
    )

    with pytest.raises(ContainerError) as caught:
        run_git(
            docker_client,  # type: ignore[arg-type]
            image="alpine/git:latest",
            volumes={},
            script="git fetch",
        )

    assert caught.value.exit_status == 128
    assert caught.value.stderr == "fatal: no remote\n"


def test_a_timed_out_git_container_is_not_reported_as_an_exit_status() -> None:
    """A kill leaves no exit code, so the ContainerError path would say "None"."""
    docker_client = _SpyDockerClient()
    docker_client.containers = _SpyContainers(timed_out=True)

    with pytest.raises(GitTimeoutError, match="exceeded"):
        run_git(
            docker_client,  # type: ignore[arg-type]
            image="alpine/git:latest",
            volumes={},
            script="git fetch",
        )


def test_git_timeout_setting_uses_the_controller_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTROLLER_GIT_TIMEOUT_SECONDS", "123")
    get_controller_settings.cache_clear()
    try:
        assert get_controller_settings().git_timeout_seconds == 123
    finally:
        get_controller_settings.cache_clear()


def test_mirror_staleness_count_is_local_read_only_and_credential_free() -> None:
    docker_client = _SpyDockerClient()
    docker_client.containers = _SpyContainers(output=b"12\n")

    count = count_mirror_staleness(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        mirror_volume="project-mirror",
        current_base_commit="a" * 40,
        base_ref="refs/heads/main",
    )

    assert count == 12
    call = docker_client.containers.calls[0]
    assert call["network_mode"] == "none"
    assert call["volumes"] == {"project-mirror": {"bind": "/mirror", "mode": "ro"}}
    assert call["environment"]["ORCHESTRATOR_CURRENT_BASE_COMMIT"] == "a" * 40
    assert call["environment"]["ORCHESTRATOR_BASE_REF"] == "refs/heads/main"


def test_run_git_pulls_an_image_only_when_requested() -> None:
    docker_client = _SpyDockerClient()

    run_git(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={},
        script="git status",
    )
    run_git(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={},
        script="git status",
        ensure_image=True,
    )

    assert docker_client.images.get_calls == ["alpine/git:latest"]
    assert docker_client.images.pull_calls == ["alpine/git:latest"]


def test_canonical_fetch_keeps_token_out_of_environment_command_and_labels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = "github-read-token-that-must-not-leak"
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    fetch_canonical_mirror(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        mirror_volume="project-mirror",
        credential_source=_StaticCredentialSource(token),
    )

    call = docker_client.containers.calls[0]
    assert token not in call["environment"].values()
    assert token not in " ".join(call["command"])
    assert call["labels"] == {}
    assert "network_mode" not in call
    assert call["volumes"]["project-mirror"] == {"bind": "/mirror", "mode": "rw"}
    assert "remote.origin.fetch '+refs/*:refs/*'" in call["command"][0]
    assert "config --unset-all remote.origin.mirror || true" in call["command"][0]
    secret_mounts = [
        (host_path, mount)
        for host_path, mount in call["volumes"].items()
        if mount["bind"] == GITHUB_READ_TOKEN_PATH
    ]
    assert len(secret_mounts) == 1
    assert secret_mounts[0][1]["mode"] == "ro"
    assert set(call["volumes"]) == {"project-mirror", secret_mounts[0][0]}
    assert not Path(secret_mounts[0][0]).exists()
    assert not Path(secret_mounts[0][0]).parent.exists()


def test_credential_file_permissions_and_finally_cleanup_on_container_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _FailingContainers(_SpyContainers):
        def create(self, **kwargs: Any) -> _SpyContainer:
            self.calls.append(kwargs)
            secret_path = next(
                Path(host_path)
                for host_path, mount in kwargs["volumes"].items()
                if mount["bind"] == GITHUB_READ_TOKEN_PATH
            )
            assert secret_path.is_file()
            assert secret_path.read_text() == "test-token"
            assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(secret_path.parent.stat().st_mode) == 0o700
            raise RuntimeError("injected container failure")

    docker_client = _SpyDockerClient()
    docker_client.containers = _FailingContainers()
    secret_directory = tmp_path / "run-secrets"
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(secret_directory))
    get_controller_settings.cache_clear()

    with pytest.raises(RuntimeError, match="injected container failure"):
        fetch_canonical_mirror(
            docker_client,  # type: ignore[arg-type]
            image="alpine/git:latest",
            mirror_volume="project-mirror",
            credential_source=_StaticCredentialSource("test-token"),
        )

    assert list(secret_directory.glob("github-read-*/github_read_token")) == []
    assert list(secret_directory.glob("github-read-*")) == []


def test_credentials_extend_git_config_without_reenabling_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    run_git(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={},
        script="git fetch",
        network=GitNetworkMode.ENABLED,
        credential_source=_StaticCredentialSource("test-token"),
    )

    environment = docker_client.containers.calls[0]["environment"]
    assert environment["GIT_CONFIG_COUNT"] == "2"
    assert environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert "github_read_token" in environment["GIT_CONFIG_VALUE_1"]


def test_write_credentials_extend_git_config_without_reenabling_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    run_git_with_write_credentials(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={"project-mirror": {"bind": "/mirror", "mode": "rw"}},
        script="git push origin feature/test",
        credential_source=_StaticWriteCredentialSource("write-token"),
    )

    call = docker_client.containers.calls[0]
    environment = call["environment"]
    assert environment["GIT_CONFIG_COUNT"] == "2"
    assert environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert "github_write_token" in environment["GIT_CONFIG_VALUE_1"]
    assert "write-token" not in environment.values()
    assert "write-token" not in " ".join(call["command"])
    assert call["labels"] == {}
    secret_mount = next(
        mount
        for mount in call["volumes"].values()
        if mount["bind"] == GITHUB_WRITE_TOKEN_PATH
    )
    assert secret_mount["mode"] == "ro"


def test_write_path_requires_a_write_token_not_a_read_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    monkeypatch.setenv(GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE, "read-only-token")
    monkeypatch.delenv(GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE, raising=False)
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    with pytest.raises(
        GitCredentialError,
        match=f"Set {GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE} in the controller environment",
    ):
        push_mirror_to_remote(
            docker_client,  # type: ignore[arg-type]
            image="alpine/git:latest",
            mirror_volume="project-mirror",
            remote_branch="feature/test",
        )

    assert docker_client.containers.calls == []


def test_write_credential_file_uses_a_distinct_private_path_and_is_cleaned(
    tmp_path: Path,
) -> None:
    secret_directory = tmp_path / "run-secrets"

    with provision_git_write_token(
        _StaticWriteCredentialSource("write-token"), secret_directory
    ) as secret_path:
        assert secret_path.name == "github_write_token"
        assert secret_path.parent.name.startswith("github-write-")
        assert secret_path.read_text() == "write-token"
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(secret_path.parent.stat().st_mode) == 0o700

    assert list(secret_directory.glob("github-write-*/github_write_token")) == []
    assert list(secret_directory.glob("github-write-*")) == []


def test_credential_requires_explicit_network_enablement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    with pytest.raises(GitCredentialError, match="GitNetworkMode.ENABLED explicitly"):
        run_git(
            docker_client,  # type: ignore[arg-type]
            image="alpine/git:latest",
            volumes={},
            script="git fetch",
            credential_source=_StaticCredentialSource("test-token"),
        )

    assert docker_client.containers.calls == []


def test_missing_environment_token_stops_before_container_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    with pytest.raises(
        GitCredentialError,
        match=f"Set {GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE} in the controller environment",
    ):
        fetch_canonical_mirror(
            docker_client,  # type: ignore[arg-type]
            image="alpine/git:latest",
            mirror_volume="project-mirror",
        )

    assert docker_client.containers.calls == []


def test_default_secret_directory_is_docker_shared_and_outside_repository() -> None:
    get_controller_settings.cache_clear()
    secret_directory = get_controller_settings().git_secret_directory

    assert secret_directory == (Path.home() / ".orchestrator" / "run-secrets").resolve()
    assert not secret_directory.is_relative_to(Path.cwd().resolve())
    assert not secret_directory.is_relative_to(Path("/tmp"))
    assert not secret_directory.is_relative_to(Path("/private/tmp"))
    assert not secret_directory.is_relative_to(Path("/var/folders"))


def test_credential_mount_check_reports_docker_desktop_empty_directory_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", str(tmp_path / "run-secrets"))
    get_controller_settings.cache_clear()
    docker_client = _SpyDockerClient()

    run_git(
        docker_client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        volumes={},
        script="git fetch",
        network=GitNetworkMode.ENABLED,
        credential_source=_StaticCredentialSource("test-token"),
    )

    script = docker_client.containers.calls[0]["command"][0]
    assert (
        f"[ ! -f {GITHUB_READ_TOKEN_PATH} ] || [ ! -s {GITHUB_READ_TOKEN_PATH} ]"
        in script
    )
    assert "GitHub read credential mount failed" in script


@requires_docker
def test_credential_file_is_readable_inside_fetch_container_and_not_in_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    mirror_volume = client.volumes.create(name=f"orchestrator-git-mirror-{run_id[:12]}")
    remote_volume = client.volumes.create(name=f"orchestrator-git-remote-{run_id[:12]}")
    with tempfile.TemporaryDirectory(dir=Path.home()) as secret_directory:
        monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", secret_directory)
        get_controller_settings.cache_clear()
        try:
            client.containers.run(
                "alpine/git:latest",
                entrypoint=["sh", "-c"],
                command=[
                    (
                        "set -eu\n"
                        "git init --bare -q /remote/repository.git\n"
                        "git init --bare -q /mirror\n"
                        "git -C /mirror remote add origin /remote/repository.git\n"
                    )
                ],
                remove=True,
                volumes={
                    mirror_volume.name: {"bind": "/mirror", "mode": "rw"},
                    remote_volume.name: {"bind": "/remote", "mode": "rw"},
                },
                tmpfs={"/git": "rw,nosuid,size=1m"},
            )

            output = run_git(
                client,
                image="alpine/git:latest",
                volumes={
                    mirror_volume.name: {"bind": "/mirror", "mode": "rw"},
                    remote_volume.name: {"bind": "/remote", "mode": "ro"},
                },
                script=(
                    f"test -r {GITHUB_READ_TOKEN_PATH}\n"
                    f"test -s {GITHUB_READ_TOKEN_PATH}\n"
                    f"! env | grep -q '^${GITHUB_READ_TOKEN_ENVIRONMENT_VARIABLE}='\n"
                    "git -C /mirror fetch origin\n"
                    "printf fetched"
                ),
                network=GitNetworkMode.ENABLED,
                credential_source=_StaticCredentialSource("test-only-read-token"),
            )

            assert output.endswith(b"fetched")
        finally:
            mirror_volume.remove(force=True)
            remote_volume.remove(force=True)


@requires_docker
def test_publish_pushes_a_reviewed_branch_through_the_mirror_to_a_local_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = docker.from_env()
    run_id = uuid4().hex[:12]
    network = remote_volume = mirror_volume = workspace_volume = daemon = None
    try:
        network = client.networks.create(name=f"orchestrator-publish-net-{run_id}")
        remote_volume = client.volumes.create(
            name=f"orchestrator-publish-remote-{run_id}"
        )
        mirror_volume = client.volumes.create(
            name=f"orchestrator-publish-mirror-{run_id}"
        )
        workspace_volume = client.volumes.create(
            name=f"orchestrator-publish-workspace-{run_id}"
        )
        client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                (
                    "set -eu\n"
                    "git init --bare -q /remote/repository.git\n"
                    "git init -q -b main /tmp/seed\n"
                    "git -C /tmp/seed config user.name test\n"
                    "git -C /tmp/seed config user.email test@example.invalid\n"
                    "printf initial > /tmp/seed/source.txt\n"
                    "git -C /tmp/seed add source.txt\n"
                    "git -C /tmp/seed commit -qm initial\n"
                    "git -C /tmp/seed remote add origin /remote/repository.git\n"
                    "git -C /tmp/seed push -q origin main\n"
                    "git -C /remote/repository.git symbolic-ref HEAD refs/heads/main\n"
                )
            ],
            remove=True,
            volumes={remote_volume.name: {"bind": "/remote", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        daemon = client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                (
                    "apk add --no-cache git-daemon >/dev/null 2>&1\n"
                    "exec git daemon --reuseaddr --export-all --enable=receive-pack "
                    "--base-path=/remote /remote\n"
                )
            ],
            detach=True,
            # rw, unlike the read-only staleness fixture: --enable=receive-pack
            # lets the daemon accept a push, but it still has to WRITE the objects
            # and refs. With a read-only mount the push fails late, as
            # "send-pack: unexpected disconnect", which reads like a credential or
            # network fault rather than a mount permission.
            volumes={remote_volume.name: {"bind": "/remote", "mode": "rw"}},
            network=network.name,
            ports={"9418/tcp": ("127.0.0.1", None)},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        daemon.reload()
        host_port = int(
            daemon.attrs["NetworkSettings"]["Ports"]["9418/tcp"][0]["HostPort"]
        )
        remote_url = f"git://host.docker.internal:{host_port}/repository.git"
        # Measured: `apk add git-daemon` needs roughly 30 of these probes
        # before the daemon accepts connections, and a budget of 30 sat exactly
        # on that boundary - the fixture failed intermittently and the symptom
        # looked like a product bug. Keep the budget well clear of it.
        for _ in range(300):
            probe = client.containers.run(
                "alpine/git:latest",
                entrypoint=["sh", "-c"],
                command=[
                    f"git ls-remote {remote_url} >/dev/null 2>&1 && echo ready || echo waiting"
                ],
                remove=True,
                tmpfs={"/git": "rw,nosuid,size=1m"},
            )
            if probe.decode().strip() == "ready":
                break
        else:
            raise AssertionError("git daemon fixture never became reachable")
        client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                (
                    "set -eu\n"
                    'git clone --mirror -q "$ORCHESTRATOR_REMOTE" /mirror\n'
                    "git -C /mirror config --unset-all remote.origin.mirror || true\n"
                )
            ],
            remove=True,
            environment={"ORCHESTRATOR_REMOTE": remote_url},
            volumes={mirror_volume.name: {"bind": "/mirror", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        base_commit = (
            client.containers.run(
                "alpine/git:latest",
                entrypoint=["sh", "-c"],
                command=["git -C /mirror rev-parse refs/heads/main"],
                remove=True,
                volumes={mirror_volume.name: {"bind": "/mirror", "mode": "ro"}},
                tmpfs={"/git": "rw,nosuid,size=1m"},
            )
            .decode()
            .strip()
        )
        clone_mirror_to_workspace(
            client,
            image="alpine/git:latest",
            mirror_volume=mirror_volume.name,
            workspace_volume=workspace_volume.name,
            base_commit=base_commit,
            branch="feature/publish-test",
            ensure_image=True,
        )
        reviewed_head = (
            client.containers.run(
                "alpine/git:latest",
                entrypoint=["sh", "-c"],
                command=[
                    (
                        "set -eu\n"
                        "git -C /workspace config user.name test\n"
                        "git -C /workspace config user.email test@example.invalid\n"
                        "printf reviewed > /workspace/reviewed.txt\n"
                        "git -C /workspace add reviewed.txt\n"
                        "git -C /workspace commit -qm reviewed\n"
                        "git -C /workspace rev-parse HEAD\n"
                    )
                ],
                remove=True,
                volumes={workspace_volume.name: {"bind": "/workspace", "mode": "rw"}},
                tmpfs={"/git": "rw,nosuid,size=1m"},
            )
            .decode()
            .strip()
        )
        mirror_commit = push_workspace_to_mirror(
            client,
            image="alpine/git:latest",
            workspace_volume=workspace_volume.name,
            mirror_volume=mirror_volume.name,
            feature_branch="feature/publish-test",
            remote_branch="feature/publish-test",
            reviewed_head=reviewed_head,
            ensure_image=True,
        )
        assert mirror_commit == reviewed_head
        with tempfile.TemporaryDirectory(dir=Path.home()) as secret_directory:
            monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", secret_directory)
            get_controller_settings.cache_clear()
            source = _StaticWriteCredentialSource("test-only-write-token")
            push_mirror_to_remote(
                client,
                image="alpine/git:latest",
                mirror_volume=mirror_volume.name,
                remote_branch="feature/publish-test",
                credential_source=source,
                ensure_image=True,
            )
            assert (
                remote_branch_sha(
                    client,
                    image="alpine/git:latest",
                    mirror_volume=mirror_volume.name,
                    remote_branch="feature/publish-test",
                    credential_source=source,
                    ensure_image=True,
                )
                == reviewed_head
            )
        remotes = client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=["git -C /workspace remote"],
            remove=True,
            volumes={workspace_volume.name: {"bind": "/workspace", "mode": "ro"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        assert remotes.decode().strip() == ""
    finally:
        get_controller_settings.cache_clear()
        if daemon is not None:
            daemon.remove(force=True)
        if workspace_volume is not None:
            workspace_volume.remove(force=True)
        if mirror_volume is not None:
            mirror_volume.remove(force=True)
        if remote_volume is not None:
            remote_volume.remove(force=True)
        if network is not None:
            network.remove()
