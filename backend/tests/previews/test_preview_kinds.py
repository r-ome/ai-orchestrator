import os
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import docker
import pytest
from conftest import register_ready_v1_sandbox

from app.controller.store import ControllerStore
from app.controller.store.task_status import TaskStatus
from app.platform.naming import ownership_labels, workspace_volume
from app.previews.config import PreviewSettings
from app.previews.dependency_cache import _run_volume_name
from app.previews.errors import PreviewOperationError
from app.previews.models import (
    PreviewAction,
    PreviewConfiguration,
    PreviewKind,
    PreviewMode,
    PreviewNetworkAccess,
    PreviewRuntime,
    StartPreviewRequest,
)
from app.previews.resources import _labels, _remove_resources
from app.previews.runtimes.native import _start_native
from app.previews.service import (
    _available_host_port,
    propose_preview,
    restart_preview,
    start_preview,
    stop_preview,
)
from app.projects.service import ensure_git_baseline
from app.tasks.models import ReportTaskRequest, StartTaskRequest
from app.tasks.service import report_task_complete, start_task

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)

GIT_IMAGE = "alpine/git:latest"
PROJECT_MANAGED = "orchestrator.project.managed"
PROJECT_NAME = "orchestrator.project.name"
LABEL_SOURCE = "orchestrator.project.source"
LABEL_CREATED_AT = "orchestrator.project.created-at"
LABEL_COPY_MODE = "orchestrator.project.copy-mode"
LABEL_FILE_COUNT = "orchestrator.project.file-count"
LABEL_COPIED_BYTES = "orchestrator.project.copied-bytes"
LABEL_EXCLUDED_DIRECTORIES = "orchestrator.project.excluded-directories"
LABEL_COPY_IMAGE = "orchestrator.project.copy-image"
LABEL_STATUS_STORAGE = "orchestrator.project.copy-status-storage"
LABEL_COPY_JOB_ID = "orchestrator.project.copy-job-id"
LABEL_PROJECT_ID = "orchestrator.project.id"
LABEL_SANDBOX_ID = "orchestrator.sandbox.id"
STATUS_STORAGE_PROJECT_VOLUME = "project-volume-v1"


def _serve(path: str, setup: str = "") -> str:
    """A start command that answers every request with the file, re-read each time."""
    return (
        "sh -c '" + setup + "while true; do "
        f"body=$(cat {path} 2>/dev/null || printf missing); "
        'printf "HTTP/1.1 200 OK\\r\\nContent-Length: ${#body}\\r\\n\\r\\n$body" '
        "| nc -l -p 8000; done'"
    )


def _settings() -> PreviewSettings:
    return PreviewSettings(
        inspection_image="alpine:latest",
        default_expiry_minutes=30,
        maximum_file_bytes=1_048_576,
        maximum_snapshot_bytes=16_777_216,
        proposal_lifetime_seconds=900,
        prepare_timeout_seconds=600,
        build_timeout_seconds=900,
    )


def _shell(client, volume: str, script: str, *, image: str = "alpine:latest") -> str:
    """Runs a throwaway shell against the sandbox volume, mounted at /project."""
    keywords = (
        {"entrypoint": ["sh", "-c"], "command": [script]}
        if image == GIT_IMAGE
        else {"command": ["sh", "-c", script]}
    )
    output = client.containers.run(
        image=image,
        remove=True,
        volumes={volume: {"bind": "/project", "mode": "rw"}},
        **keywords,
    )
    return output.decode() if isinstance(output, bytes) else str(output)


def _fetch(url: str) -> str:
    error: Exception | None = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.read().decode()
        except OSError as failure:
            error = failure
            time.sleep(0.2)
    raise AssertionError(f"{url} never answered: {error}")


def _sandbox_volume(
    client,
    project_name: str,
    sandbox_id: str,
    project_id: str,
    source: Path,
):
    return client.volumes.create(
        name=f"{workspace_volume(project_name)}-{sandbox_id[:12]}",
        labels={
            **ownership_labels(sandbox_id=project_name, project_id=project_id),
            PROJECT_MANAGED: "true",
            PROJECT_NAME: project_name,
            LABEL_SOURCE: str(source),
            LABEL_CREATED_AT: "2026-08-06T00:00:00Z",
            LABEL_COPY_MODE: "snapshot",
            LABEL_FILE_COUNT: "1",
            LABEL_COPIED_BYTES: "4",
            LABEL_EXCLUDED_DIRECTORIES: "",
            LABEL_COPY_IMAGE: "alpine:latest",
            LABEL_STATUS_STORAGE: STATUS_STORAGE_PROJECT_VOLUME,
            LABEL_COPY_JOB_ID: uuid4().hex,
            LABEL_PROJECT_ID: project_id,
            LABEL_SANDBOX_ID: project_name,
        },
    )


def _mark_copied(client, volume: str) -> None:
    _shell(
        client,
        volume,
        "set -eu\n"
        "mkdir -p /project/.orchestrator/copy-job\n"
        "printf completed > /project/.orchestrator/copy-job/status\n"
        "printf 2026-08-06T00:00:00Z > /project/.orchestrator/copy-job/started_at\n"
        "printf 2026-08-06T00:00:01Z > /project/.orchestrator/copy-job/finished_at\n"
        "printf 0 > /project/.orchestrator/copy-job/exit_code\n"
        ": > /project/.orchestrator/copy-job/error\n"
        ": > /project/.orchestrator/copy-job/copy.log\n",
    )


def _commit(client, volume: str, script: str) -> None:
    _shell(
        client,
        volume,
        "set -eu\ncd /project\n" + script,
        image=GIT_IMAGE,
    )


def test_task_preview_serves_its_commit_and_keeps_it_across_a_restart(
    tmp_path: Path,
) -> None:
    client = docker.from_env()
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    settings = _settings()
    sandbox_id = uuid4().hex
    project_name = f"task-preview-{sandbox_id[:8]}"
    project_id = uuid4().hex
    source = tmp_path / project_name
    source.mkdir()
    volume = _sandbox_volume(client, project_name, sandbox_id, project_id, source)
    register_ready_v1_sandbox(
        store,
        sandbox_id=project_name,
        project_id=project_id,
        project_name=project_name,
        volume_name=volume.name,
        created_at="2026-08-06T00:00:00Z",
        db_engine="none",
    )
    started = False
    try:
        _shell(client, volume.name, "printf base > /project/index.html")
        _mark_copied(client, volume.name)
        ensure_git_baseline(client, GIT_IMAGE, volume.name)

        task = start_task(client, store, StartTaskRequest(project_name=project_name))
        _commit(
            client,
            volume.name,
            "printf contact-v1 > contact.txt\n"
            "git add -A\n"
            'git commit -q -m "add contact"\n',
        )
        task = report_task_complete(client, store, task.id, ReportTaskRequest())
        assert task.status is TaskStatus.REPORTED
        approved_commit = task.head_commit
        assert approved_commit

        proposal = propose_preview(client, store, settings, project_name)
        port = _available_host_port()
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="alpine:latest",
            start_command=_serve("/workspace/contact.txt"),
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        run = start_preview(
            client,
            store,
            settings,
            project_name,
            StartPreviewRequest(
                proposal_id=proposal.id,
                proposal_digest=proposal.digest,
                config=config,
                action=PreviewAction.START,
                actor="integration-test",
                task_id=task.id,
            ),
        )
        started = True
        assert run.kind is PreviewKind.TASK
        assert run.commit_sha == approved_commit
        assert run.task_id == task.id
        assert _fetch(run.url) == "contact-v1"

        # The task preview is immutable: the branch moves on, and a stale
        # workspace is emptied and re-exported from the approved commit.
        _commit(
            client,
            volume.name,
            "printf contact-v2 > contact.txt\n"
            "git add -A\n"
            'git commit -q -m "later work"\n',
        )
        workspace = _run_volume_name(run.id, "runtime-workspace")
        client.containers.run(
            "alpine:latest",
            ["sh", "-c", "rm -f /workspace/contact.txt"],
            remove=True,
            volumes={workspace: {"bind": "/workspace", "mode": "rw"}},
        )

        restarted = restart_preview(client, store, settings, project_name)
        assert restarted.commit_sha == approved_commit
        assert _fetch(restarted.url) == "contact-v1"
        assert store.task(task.id)["status"] == TaskStatus.REVIEW.value
    finally:
        if started:
            try:
                stop_preview(
                    client,
                    store,
                    project_name,
                    remove_data_volumes=True,
                )
            except Exception:  # noqa: BLE001, S110 - test cleanup must not mask the assertion failure
                pass
        volume.remove(force=True)
        client.close()


def test_failed_task_preview_returns_the_task_to_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test covers task-state recovery. Environment masking has its own
    # integration coverage and Docker Desktop cannot mount pytest's private
    # temporary directory into a container on macOS.
    monkeypatch.setattr(
        "app.previews.runtimes.native._environment_masks", lambda *_: []
    )
    client = docker.from_env()
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    settings = _settings()
    sandbox_id = uuid4().hex
    project_name = f"failed-task-preview-{sandbox_id[:8]}"
    project_id = uuid4().hex
    source = tmp_path / project_name
    source.mkdir()
    volume = _sandbox_volume(client, project_name, sandbox_id, project_id, source)
    register_ready_v1_sandbox(
        store,
        sandbox_id=project_name,
        project_id=project_id,
        project_name=project_name,
        volume_name=volume.name,
        created_at="2026-08-06T00:00:00Z",
        db_engine="none",
    )
    try:
        _shell(client, volume.name, "printf base > /project/index.html")
        _mark_copied(client, volume.name)
        ensure_git_baseline(client, GIT_IMAGE, volume.name)

        task = start_task(client, store, StartTaskRequest(project_name=project_name))
        _commit(
            client,
            volume.name,
            'printf task > task.txt\ngit add -A\ngit commit -q -m "add task"\n',
        )
        task = report_task_complete(client, store, task.id, ReportTaskRequest())
        proposal = propose_preview(client, store, settings, project_name)
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="alpine:latest",
            install_command="exit 19",
            start_command="sleep 60",
            container_port=8000,
            host_port=_available_host_port(),
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )

        with pytest.raises(PreviewOperationError, match="failed with code 19"):
            start_preview(
                client,
                store,
                settings,
                project_name,
                StartPreviewRequest(
                    proposal_id=proposal.id,
                    proposal_digest=proposal.digest,
                    config=config,
                    action=PreviewAction.START,
                    actor="integration-test",
                    task_id=task.id,
                ),
            )

        assert store.task(task.id)["status"] == TaskStatus.REVIEW.value
    finally:
        volume.remove(force=True)
        client.close()


def test_live_preview_cannot_read_a_planted_environment_file() -> None:
    client = docker.from_env()
    settings = _settings()
    run_id = uuid4().hex
    volume = client.volumes.create(name=f"orchestrator-env-mask-test-{run_id[:12]}")
    resources: dict = {"containers": [], "networks": [], "volumes": []}
    try:
        _shell(
            client,
            volume.name,
            "set -eu\n"
            "printf 'API_KEY=planted-root-secret' > /project/.env\n"
            "mkdir -p /project/apps/web\n"
            "printf 'API_KEY=planted-nested-secret' > /project/apps/web/.env\n"
            "printf served > /project/index.html\n",
        )
        ensure_git_baseline(client, GIT_IMAGE, volume.name)
        port = _available_host_port()
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="alpine:latest",
            start_command=_serve("/workspace/index.html"),
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        resources = _start_native(
            client,
            settings,
            volume.name,
            config,
            _labels(f"sandbox{run_id[:8]}", run_id, "2099-01-01T00:00:00Z"),
            run_id,
            port,
        )
        application = resources["containers"][0]
        assert _fetch(f"http://127.0.0.1:{port}") == "served"

        read = application.exec_run(
            ["sh", "-c", "cat /workspace/.env /workspace/apps/web/.env"]
        )
        assert b"planted-root-secret" not in read.output
        assert b"planted-nested-secret" not in read.output

        # The coding agent writes a fresh secret after the preview started. A
        # mount, unlike a copy-time exclusion, is not defeated by that.
        _shell(
            client,
            volume.name,
            "printf 'API_KEY=written-after-start' > /project/.env",
        )
        later = application.exec_run(["cat", "/workspace/.env"])
        assert b"written-after-start" not in later.output
        assert later.output.strip() == b""

        # The sandbox itself still holds the file: the mask hides it from the
        # preview, it does not delete the project's data.
        assert "written-after-start" in _shell(client, volume.name, "cat /project/.env")
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        volume.remove(force=True)
        client.close()


def test_live_preview_build_output_stays_out_of_the_sandbox_worktree() -> None:
    client = docker.from_env()
    settings = _settings()
    run_id = uuid4().hex
    sandbox_id = f"sandbox{run_id[:8]}"
    volume = client.volumes.create(name=f"orchestrator-build-test-{run_id[:12]}")
    resources: dict = {"containers": [], "networks": [], "volumes": []}
    try:
        _shell(
            client,
            volume.name,
            "set -eu\n"
            'printf \'{"name":"blog"}\' > /project/package.json\n'
            "printf served > /project/index.html\n",
        )
        ensure_git_baseline(client, GIT_IMAGE, volume.name)
        port = _available_host_port()
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.ASTRO,
            image="alpine:latest",
            start_command=_serve(
                "/workspace/dist/out.txt",
                setup=(
                    "mkdir -p /workspace/dist /workspace/.astro; "
                    "printf built > /workspace/dist/out.txt; "
                    "printf cached > /workspace/.astro/cache.txt; "
                ),
            ),
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        resources = _start_native(
            client,
            settings,
            volume.name,
            config,
            _labels(sandbox_id, run_id, "2099-01-01T00:00:00Z"),
            run_id,
            port,
        )
        application = resources["containers"][0]
        assert _fetch(f"http://127.0.0.1:{port}") == "built"
        assert (
            application.exec_run(["cat", "/workspace/.astro/cache.txt"]).output
            == b"cached"
        )

        # Phase 2's dirty-tree rule rejects a completion report on any untracked
        # path, so neither the build output nor the env masks may appear here.
        status = _shell(
            client,
            volume.name,
            "cd /project && git status --porcelain",
            image=GIT_IMAGE,
        )
        assert status.strip() == "", status
        assert (
            "no such file"
            in _shell(
                client,
                volume.name,
                "cat /project/dist/out.txt 2>&1 || true",
            ).casefold()
        )
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        for stale in client.volumes.list(
            filters={"name": f"orchestrator-deps-{sandbox_id[:12]}"}
        ):
            try:
                stale.remove(force=True)
            except Exception:  # noqa: BLE001, S110 - test cleanup must not mask the assertion failure
                pass
        volume.remove(force=True)
        client.close()
