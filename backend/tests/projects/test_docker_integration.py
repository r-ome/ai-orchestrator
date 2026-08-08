import os
import time
from pathlib import Path

import docker
import pytest
from docker.errors import NotFound

from app.projects.config import ProjectSettings
from app.projects.models import CopyProjectRequest
from app.projects.service import (
    COPY_CONTAINER_PREFIX,
    LABEL_METADATA_VOLUME,
    LABEL_STATUS_STORAGE,
    STATUS_STORAGE_CONTROLLER_VOLUME,
    _copy_job_from_volume,
    register_project,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)


def _wait_for_copy_to_finish(client: docker.DockerClient, container_name: str) -> None:
    for _ in range(100):
        try:
            container = client.containers.get(container_name)
            container.reload()
        except NotFound:
            # auto_remove=True: the container can disappear between the
            # get() and the reload() the instant it exits.
            return
        if container.status == "exited":
            return
        time.sleep(0.1)
    pytest.fail("copy job did not finish in time")


def test_copy_metadata_lives_outside_the_sandbox_volume(tmp_path: Path) -> None:
    """Proves the trust boundary moved.

    Trusted metadata (CONTEXT.md) is controller-owned state that a coding
    agent cannot modify. Before this change the copy job wrote its status
    under `.orchestrator` inside the same volume an agent mounts read-write.
    This asserts that path is gone from the sandbox volume, and that the
    real bookkeeping lives in a separate, controller-owned volume instead.
    """
    client = docker.from_env()
    source = tmp_path / "sample-project"
    source.mkdir()
    (source / "README.md").write_text("hello", encoding="utf-8")
    settings = ProjectSettings(projects_root=tmp_path, copy_image="alpine:latest")

    job = register_project(client, settings, CopyProjectRequest(path=str(source)))
    volume = client.volumes.get(job.volume_name)
    metadata_volume_name = (volume.attrs.get("Labels") or {}).get(LABEL_METADATA_VOLUME)
    assert metadata_volume_name

    try:
        _wait_for_copy_to_finish(client, f"{COPY_CONTAINER_PREFIX}{job.job_id}")

        sandbox_listing = client.containers.run(
            "alpine:latest",
            ["find", "/project", "-mindepth", "1"],
            remove=True,
            volumes={job.volume_name: {"bind": "/project", "mode": "ro"}},
        )
        sandbox_paths = sandbox_listing.decode().splitlines()
        assert not any(".orchestrator" in path for path in sandbox_paths)

        metadata_listing = client.containers.run(
            "alpine:latest",
            ["cat", "/controller/status"],
            remove=True,
            volumes={metadata_volume_name: {"bind": "/controller", "mode": "ro"}},
        )
        assert metadata_listing.decode().strip() == "completed"

        volume.reload()
        labels = volume.attrs.get("Labels") or {}
        assert labels[LABEL_METADATA_VOLUME] == metadata_volume_name

        status = _copy_job_from_volume(client, volume, include_logs=True)
        assert status.status == "completed"
        assert status.ready is True
        assert "copy completed" in status.log_tail
    finally:
        try:
            client.containers.get(f"{COPY_CONTAINER_PREFIX}{job.job_id}").remove(force=True)
        except NotFound:
            pass
        volume.remove(force=True)
        try:
            client.volumes.get(metadata_volume_name).remove(force=True)
        except NotFound:
            pass


def test_registration_labels_new_jobs_with_controller_volume_storage(
    tmp_path: Path,
) -> None:
    client = docker.from_env()
    source = tmp_path / "sample-project"
    source.mkdir()
    settings = ProjectSettings(projects_root=tmp_path, copy_image="alpine:latest")

    job = register_project(client, settings, CopyProjectRequest(path=str(source)))
    volume = client.volumes.get(job.volume_name)
    labels = volume.attrs.get("Labels") or {}
    metadata_volume_name = labels.get(LABEL_METADATA_VOLUME, "")

    try:
        assert labels[LABEL_STATUS_STORAGE] == STATUS_STORAGE_CONTROLLER_VOLUME
        # The metadata volume exists as its own resource, independent of the
        # sandbox volume.
        assert client.volumes.get(metadata_volume_name).name == metadata_volume_name
    finally:
        _wait_for_copy_to_finish(client, f"{COPY_CONTAINER_PREFIX}{job.job_id}")
        try:
            client.containers.get(f"{COPY_CONTAINER_PREFIX}{job.job_id}").remove(force=True)
        except NotFound:
            pass
        volume.remove(force=True)
        if metadata_volume_name:
            try:
                client.volumes.get(metadata_volume_name).remove(force=True)
            except NotFound:
                pass
