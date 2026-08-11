import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker
import pytest

from app.controller.config import get_controller_settings
from app.sandboxes.git import clone_mirror_to_workspace, ensure_canonical_mirror
from app.sandboxes.mirror import MirrorPin, ensure_project_mirror, ensure_workspace_import
from app.sandboxes.naming import (
    mirror_ownership_labels,
    mirror_volume,
    ownership_labels,
    sandbox_id_for,
)


PROJECT_ID = "a" * 32
SANDBOX_ID = sandbox_id_for(PROJECT_ID, "mirror-import")
requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


def test_project_mirror_is_get_or_validate_and_has_no_sandbox_label(
    fake_docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fetch(_client, **kwargs: Any) -> tuple[str, str]:
        calls.append(kwargs)
        return "main", "c" * 40

    monkeypatch.setattr("app.sandboxes.mirror.ensure_canonical_mirror", fetch)

    first = ensure_project_mirror(
        fake_docker_client,
        image="alpine/git:latest",
        project_id=PROJECT_ID,
        remote_url="https://github.com/owner/repo",
    )
    second = ensure_project_mirror(
        fake_docker_client,
        image="alpine/git:latest",
        project_id=PROJECT_ID,
        remote_url="https://github.com/owner/repo",
    )

    assert first == second
    assert len(fake_docker_client.volumes.items) == 1
    volume = fake_docker_client.volumes.items[0]
    assert volume.name == mirror_volume(PROJECT_ID)
    assert volume.labels == mirror_ownership_labels(project_id=PROJECT_ID)
    assert "orchestrator.sandbox.id" not in volume.labels
    assert len(calls) == 2


def test_same_named_mirror_with_wrong_labels_is_refused(
    fake_docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker_client.volumes.create(
        name=mirror_volume(PROJECT_ID), driver="local", labels={}
    )
    monkeypatch.setattr("app.sandboxes.mirror.ensure_canonical_mirror", lambda *_a, **_k: ("main", "c" * 40))

    with pytest.raises(ValueError, match="ownership"):
        ensure_project_mirror(
            fake_docker_client,
            image="alpine/git:latest",
            project_id=PROJECT_ID,
            remote_url="https://github.com/owner/repo",
        )


def test_workspace_clone_script_uses_no_local_and_strips_remotes_and_hooks() -> None:
    class Containers:
        def __init__(self) -> None:
            self.call: dict[str, Any] | None = None

        def run(self, **kwargs: Any) -> bytes:
            self.call = kwargs
            return b""

    class Client:
        def __init__(self) -> None:
            self.containers = Containers()

    client = Client()
    clone_mirror_to_workspace(
        client,  # type: ignore[arg-type]
        image="alpine/git:latest",
        mirror_volume="project-mirror",
        workspace_volume="sandbox-workspace",
        base_commit="b" * 40,
        branch="feature/mirror-import",
    )

    assert client.containers.call is not None
    call = client.containers.call
    script = call["command"][0]
    assert "git clone --no-local /mirror /workspace" in script
    assert "git remote remove" in script
    assert "find .git/hooks" in script
    assert call["network_disabled"] is True
    assert call["volumes"] == {
        "project-mirror": {"bind": "/mirror", "mode": "ro"},
        "sandbox-workspace": {"bind": "/workspace", "mode": "rw"},
    }


def test_workspace_name_with_wrong_ownership_is_not_adopted(
    fake_docker_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sandboxes.naming import workspace_volume

    fake_docker_client.volumes.create(
        name=workspace_volume(SANDBOX_ID),
        driver="local",
        labels=ownership_labels(sandbox_id="wrong", project_id=PROJECT_ID),
    )
    monkeypatch.setattr("app.sandboxes.mirror.clone_mirror_to_workspace", lambda *_a, **_k: b"")
    mirror = MirrorPin(mirror_volume(PROJECT_ID), "main", "c" * 40)

    with pytest.raises(ValueError, match="ownership"):
        ensure_workspace_import(
            fake_docker_client,
            image="alpine/git:latest",
            sandbox_id=SANDBOX_ID,
            project_id=PROJECT_ID,
            mirror=mirror,
            feature_branch="feature/mirror-import",
        )


@requires_docker
def test_workspace_import_has_no_network_remote_hooks_or_shared_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = docker.from_env()
    run_id = uuid4().hex[:12]
    network = remote_volume = mirror = workspace = daemon = None

    class StaticCredentialSource:
        def read_token(self) -> str | None:
            return "test-read-token"

    try:
        network = client.networks.create(name=f"orchestrator-phase4-net-{run_id}")
        remote_volume = client.volumes.create(name=f"orchestrator-phase4-remote-{run_id}")
        mirror = client.volumes.create(name=f"orchestrator-phase4-mirror-{run_id}")
        workspace = client.volumes.create(name=f"orchestrator-phase4-workspace-{run_id}")
        base_commit = client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "git init --bare -q /remote/repository.git\n"
                "git init -q -b main /tmp/seed\n"
                "git -C /tmp/seed config user.name test\n"
                "git -C /tmp/seed config user.email test@example.invalid\n"
                "printf source > /tmp/seed/source.txt\n"
                "git -C /tmp/seed add source.txt\n"
                "git -C /tmp/seed commit -qm source\n"
                "git -C /tmp/seed remote add origin /remote/repository.git\n"
                "git -C /tmp/seed push -q origin main\n"
                "git -C /remote/repository.git symbolic-ref HEAD refs/heads/main\n"
                "git -C /remote/repository.git rev-parse refs/heads/main\n"
            ],
            remove=True,
            volumes={remote_volume.name: {"bind": "/remote", "mode": "rw"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        ).decode().strip()
        daemon = client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "apk add --no-cache git-daemon >/dev/null 2>&1\n"
                "exec git daemon --reuseaddr --export-all --base-path=/remote /remote\n"
            ],
            detach=True,
            volumes={remote_volume.name: {"bind": "/remote", "mode": "ro"}},
            network=network.name,
            ports={"9418/tcp": ("127.0.0.1", None)},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        daemon.reload()
        port = int(daemon.attrs["NetworkSettings"]["Ports"]["9418/tcp"][0]["HostPort"])
        remote_url = f"git://host.docker.internal:{port}/repository.git"
        # Measured: `apk add git-daemon` needs roughly 30 of these probes
        # before the daemon accepts connections, and a budget of 30 sat exactly
        # on that boundary - the fixture failed intermittently and the symptom
        # looked like a product bug. Keep the budget well clear of it.
        for _ in range(300):
            probe = client.containers.run(
                "alpine/git:latest",
                entrypoint=["sh", "-c"],
                command=[f"git ls-remote {remote_url} >/dev/null 2>&1 && echo ready || echo waiting"],
                remove=True,
                tmpfs={"/git": "rw,nosuid,size=1m"},
            )
            if probe.decode().strip() == "ready":
                break
        else:
            raise AssertionError("git daemon fixture never became reachable")
        with tempfile.TemporaryDirectory(dir=Path.home()) as secret_directory:
            monkeypatch.setenv("CONTROLLER_GIT_SECRET_DIRECTORY", secret_directory)
            get_controller_settings.cache_clear()
            branch, commit = ensure_canonical_mirror(
                client,
                image="alpine/git:latest",
                mirror_volume=mirror.name,
                remote_url=remote_url,
                credential_source=StaticCredentialSource(),
                ensure_image=True,
            )
            assert (branch, commit) == ("main", base_commit)
            # The second call validates and fetches the existing mirror. It must
            # not try to clone into the populated volume again.
            assert ensure_canonical_mirror(
                client,
                image="alpine/git:latest",
                mirror_volume=mirror.name,
                remote_url=remote_url,
                credential_source=StaticCredentialSource(),
            ) == ("main", base_commit)
        get_controller_settings.cache_clear()
        mirror_shape = client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                f"test \"$(git -C /mirror rev-parse refs/heads/main)\" = {commit}\n"
                "test \"$(git -C /mirror symbolic-ref HEAD)\" = refs/heads/main\n"
                "test \"$(git -C /mirror config --get-all remote.origin.fetch)\" = '+refs/*:refs/*'\n"
                # remote.origin.mirror conflicts with the single-ref publish push.
                "! git -C /mirror config --get remote.origin.mirror\n"
                "test -z \"$(git -C /mirror for-each-ref --format='%(refname)' refs/remotes/origin)\"\n"
                "printf mirrored\n"
            ],
            remove=True,
            volumes={mirror.name: {"bind": "/mirror", "mode": "ro"}},
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        assert mirror_shape.strip() == b"mirrored"

        clone_mirror_to_workspace(
            client,
            image="alpine/git:latest",
            mirror_volume=mirror.name,
            workspace_volume=workspace.name,
            base_commit=commit,
            branch="feature/mirror-import",
        )

        verified = client.containers.run(
            "alpine/git:latest",
            entrypoint=["sh", "-c"],
            command=[
                "set -eu\n"
                "test -z \"$(git -C /workspace remote)\"\n"
                "test -z \"$(find /workspace/.git/hooks -mindepth 1 -print -quit)\"\n"
                f"test \"$(git -C /workspace rev-parse HEAD)\" = {commit}\n"
                # The real proof that --no-local was used. `git clone --local`
                # hardlinks the source object and pack files, so those entries
                # would report a link count of 2. --no-local copies them over the
                # regular transport, so every object file is unshared at count 1.
                # Writing a NEW file into the clone and finding it absent from the
                # mirror proves nothing here: hardlinks are per file, so a fresh
                # file is unshared either way.
                "links=$(find /workspace/.git/objects -type f -exec stat -c '%h' {} \\; "
                "| sort -u | tr '\\n' ',')\n"
                "test \"$links\" = '1,'\n"
                "printf clone-only > /workspace/.git/objects/clone-only-object\n"
                "test ! -e /mirror/objects/clone-only-object\n"
                "printf verified\n"
            ],
            remove=True,
            volumes={
                mirror.name: {"bind": "/mirror", "mode": "ro"},
                workspace.name: {"bind": "/workspace", "mode": "rw"},
            },
            tmpfs={"/git": "rw,nosuid,size=1m"},
        )
        assert verified.strip() == b"verified"
    finally:
        get_controller_settings.cache_clear()
        if daemon is not None:
            daemon.remove(force=True)
        if workspace is not None:
            workspace.remove(force=True)
        if mirror is not None:
            mirror.remove(force=True)
        if remote_volume is not None:
            remote_volume.remove(force=True)
        if network is not None:
            network.remove()
