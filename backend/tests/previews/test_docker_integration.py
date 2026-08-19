import os
import json
import threading
import time
import urllib.request
from uuid import uuid4
from pathlib import Path
from typing import Any

import docker
import pytest
import yaml
from fastapi.testclient import TestClient

from app.previews.config import PreviewSettings
from app.previews.models import (
    PreviewAction,
    PreviewConfiguration,
    PreviewMode,
    PreviewNetworkAccess,
    PreviewRuntime,
    StartPreviewRequest,
)
from app.dependency_cache import _volume_runtime_files
from app.previews.resources import _labels, _remove_resources
from app.previews.service import (
    _available_host_port,
    _start_compose,
    _start_dockerfile,
    _start_native,
    preview_creation_logs,
    preview_logs,
    propose_preview,
    start_preview,
    stop_preview,
)
from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.main import app
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


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
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


def test_native_preview_publishes_only_to_loopback() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    project_volume = client.volumes.create(
        name=f"orchestrator-preview-test-{run_id[:12]}"
    )
    resources = {"containers": [], "networks": [], "volumes": []}
    try:
        client.containers.run(
            "alpine:latest",
            ["sh", "-c", "printf 'preview-ready' > /workspace/index.html"],
            remove=True,
            volumes={project_volume.name: {"bind": "/workspace", "mode": "rw"}},
        )
        port = _available_host_port()
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="alpine:latest",
            start_command=(
                "sh -c 'while true; do printf \"HTTP/1.1 200 OK\\r\\n"
                "Content-Length: 13\\r\\n\\r\\npreview-ready\" | "
                "nc -l -p 8000; done'"
            ),
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        settings = PreviewSettings(
            inspection_image="alpine:latest",
            default_expiry_minutes=30,
            maximum_file_bytes=1_048_576,
            maximum_snapshot_bytes=16_777_216,
            proposal_lifetime_seconds=900,
            prepare_timeout_seconds=600,
            build_timeout_seconds=900,
        )
        inspected = _volume_runtime_files(client, project_volume.name, settings)
        assert inspected["index.html"] == b"preview-ready"
        labels = _labels("test-sandbox", run_id, "2099-01-01T00:00:00Z")

        resources = _start_native(
            client,
            settings,
            project_volume.name,
            config,
            labels,
            run_id,
            port,
        )

        body = ""
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/index.html",
                    timeout=1,
                ) as response:
                    body = response.read().decode()
                break
            except OSError:
                time.sleep(0.1)

        containers = resources["containers"]
        for container in containers:
            container.reload()
        gateway = containers[-1]
        logs = "\n".join(
            container.logs().decode("utf-8", errors="replace")
            for container in containers
        )
        assert body == "preview-ready", {
            "status": [container.status for container in containers],
            "logs": logs,
            "ports": gateway.attrs["NetworkSettings"]["Ports"],
        }
        binding = gateway.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]
        assert binding["HostIp"] == "127.0.0.1"
        application = containers[0]
        assert len(application.attrs["NetworkSettings"]["Networks"]) == 1
        assert len(gateway.attrs["NetworkSettings"]["Networks"]) == 2
        outbound = application.exec_run(
            ["wget", "-T", "2", "-q", "-O", "-", "http://1.1.1.1"]
        )
        assert outbound.exit_code != 0
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        project_volume.remove(force=True)
        client.close()


def test_native_mysql_waits_for_initialization_and_hides_environment_files() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    project_volume = client.volumes.create(
        name=f"orchestrator-mysql-preview-test-{run_id[:12]}"
    )
    resources = {"containers": [], "networks": [], "volumes": []}
    try:
        client.containers.run(
            "alpine:latest",
            [
                "sh",
                "-c",
                (
                    "set -eu; printf secret > /workspace/.env; "
                    "printf local-secret > /workspace/.env.local; "
                    "printf source > /workspace/source.txt"
                ),
            ],
            remove=True,
            volumes={project_volume.name: {"bind": "/workspace", "mode": "rw"}},
        )
        port = _available_host_port()
        response_command = (
            "sh -c 'test -f /workspace/initialized; "
            "echo application-ready; "
            "while true; do printf \"HTTP/1.1 200 OK\\r\\nContent-Length: 11"
            "\\r\\n\\r\\nmysql-ready\" | nc -l -p 8000; done'"
        )
        config = PreviewConfiguration.model_validate(
            {
                "mode": "native",
                "runtime": "unknown",
                "image": "alpine:latest",
                "start_command": response_command,
                "container_port": 8000,
                "host_port": port,
                "network_access": "isolated",
                "expiry_minutes": 30,
                "services": {
                    "database": {
                        "type": "mysql",
                        "image": "mysql:8.4",
                        "database": "atc_preview",
                        "persistence": "ephemeral",
                    }
                },
                "initialize": {
                    "commands": [
                        # A live preview mounts the sandbox, so the env files
                        # are masked in place rather than left out of a copy.
                        "test ! -s /workspace/.env",
                        "test ! -s /workspace/.env.local",
                        "test -s /workspace/source.txt",
                        "printf initialized > /workspace/initialized",
                        "echo initialization-ready",
                    ]
                },
                "environment": {
                    "DATABASE_URL": {"from_service": "database"}
                },
            }
        )
        settings = PreviewSettings(
            inspection_image="alpine:latest",
            default_expiry_minutes=30,
            maximum_file_bytes=1_048_576,
            maximum_snapshot_bytes=16_777_216,
            proposal_lifetime_seconds=900,
            prepare_timeout_seconds=120,
            build_timeout_seconds=900,
        )
        labels = _labels("test-sandbox", run_id, "2099-01-01T00:00:00Z")

        resources = _start_native(
            client,
            settings,
            project_volume.name,
            config,
            labels,
            run_id,
            port,
        )

        by_service = {}
        for container in resources["containers"]:
            container.reload()
            service = container.attrs["Config"]["Labels"][
                "orchestrator.preview.service"
            ]
            by_service[service] = container
        database = by_service["database"]
        initializer = by_service["initialize"]
        application = by_service["app"]
        published_database_ports = database.attrs["NetworkSettings"]["Ports"] or {}
        application_environment = application.attrs["Config"]["Env"] or []

        body = ""
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/",
                    timeout=1,
                ) as response:
                    body = response.read().decode()
                break
            except OSError:
                time.sleep(0.1)

        assert body == "mysql-ready"
        assert database.attrs["State"]["Health"]["Status"] == "healthy"
        assert not any(published_database_ports.values())
        assert initializer.attrs["State"]["ExitCode"] == 0
        assert "initialization-ready" in initializer.logs().decode()
        assert "application-ready" in application.logs().decode()
        assert any(
            value.startswith("DATABASE_URL=mysql://")
            and "@database:3306/atc_preview" in value
            for value in application_environment
        )
        assert application.exec_run(["test", "!", "-s", "/workspace/.env"]).exit_code == 0
        assert (
            application.exec_run(
                ["test", "!", "-s", "/workspace/.env.local"]
            ).exit_code
            == 0
        )
        assert b"secret" not in application.exec_run(
            ["cat", "/workspace/.env", "/workspace/.env.local"]
        ).output
        assert resources["networks"][0].attrs["Internal"] is True
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        project_volume.remove(force=True)
        client.close()


def test_compose_preview_starts_multiple_services_and_exposes_one() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    project_volume = client.volumes.create(
        name=f"orchestrator-compose-test-{run_id[:12]}"
    )
    resources = {"containers": [], "networks": [], "volumes": []}
    try:
        port = _available_host_port()
        response_command = (
            "while true; do printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 13"
            "\\r\\n\\r\\ncompose-ready' | nc -l -p 8000; done"
        )
        compose = {
            "services": {
                "database": {
                    "image": "alpine:latest",
                    "command": ["sleep", "300"],
                },
                "web": {
                    "image": "alpine:latest",
                    "depends_on": ["database"],
                    "command": ["sh", "-c", response_command],
                    "ports": ["8000:8000"],
                },
            }
        }
        compose_bytes = yaml.safe_dump(compose).encode()
        config = PreviewConfiguration(
            mode=PreviewMode.COMPOSE,
            runtime=PreviewRuntime.UNKNOWN,
            compose_file="compose.yaml",
            selected_service="web",
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        settings = PreviewSettings(
            inspection_image="alpine:latest",
            default_expiry_minutes=30,
            maximum_file_bytes=1_048_576,
            maximum_snapshot_bytes=16_777_216,
            proposal_lifetime_seconds=900,
            prepare_timeout_seconds=600,
            build_timeout_seconds=900,
        )
        labels = _labels("test-sandbox", run_id, "2099-01-01T00:00:00Z")

        resources = _start_compose(
            client,
            settings,
            project_volume.name,
            {"compose.yaml": compose_bytes},
            config,
            labels,
            run_id,
            port,
        )

        body = ""
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/",
                    timeout=1,
                ) as response:
                    body = response.read().decode()
                break
            except OSError:
                time.sleep(0.1)

        assert body == "compose-ready"
        assert len(resources["containers"]) == 3
        published = []
        for container in resources["containers"]:
            container.reload()
            ports = container.attrs["NetworkSettings"]["Ports"] or {}
            published.extend(binding for binding in ports.values() if binding)
        assert len(published) == 1
        assert published[0][0]["HostIp"] == "127.0.0.1"
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        project_volume.remove(force=True)
        client.close()


def test_dockerfile_preview_builds_current_sandbox_and_cleans_its_image() -> None:
    client = docker.from_env()
    run_id = uuid4().hex
    project_volume = client.volumes.create(
        name=f"orchestrator-dockerfile-test-{run_id[:12]}"
    )
    resources = {"containers": [], "networks": [], "volumes": [], "images": []}
    tag = f"orchestrator-preview:{run_id}"
    try:
        port = _available_host_port()
        response_command = (
            "while true; do printf 'HTTP/1.1 200 OK\\r\\nContent-Length: 16"
            "\\r\\n\\r\\ndockerfile-ready' | nc -l -p 8000; done"
        )
        dockerfile = (
            "FROM alpine:latest\n"
            "EXPOSE 8000\n"
            f"CMD {json.dumps(['sh', '-c', response_command])}\n"
        )
        client.containers.run(
            "alpine:latest",
            [
                "sh",
                "-c",
                "printf '%s' \"$DOCKERFILE\" > /workspace/Dockerfile",
            ],
            environment={"DOCKERFILE": dockerfile},
            remove=True,
            volumes={project_volume.name: {"bind": "/workspace", "mode": "rw"}},
        )
        config = PreviewConfiguration(
            mode=PreviewMode.DOCKERFILE,
            runtime=PreviewRuntime.UNKNOWN,
            dockerfile="Dockerfile",
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        settings = PreviewSettings(
            inspection_image="alpine:latest",
            default_expiry_minutes=30,
            maximum_file_bytes=1_048_576,
            maximum_snapshot_bytes=16_777_216,
            proposal_lifetime_seconds=900,
            prepare_timeout_seconds=600,
            build_timeout_seconds=900,
        )
        labels = _labels("test-sandbox", run_id, "2099-01-01T00:00:00Z")

        resources = _start_dockerfile(
            client,
            settings,
            project_volume.name,
            config,
            labels,
            run_id,
            port,
        )

        body = ""
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/",
                    timeout=1,
                ) as response:
                    body = response.read().decode()
                break
            except OSError:
                time.sleep(0.1)
        assert body == "dockerfile-ready"
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        project_volume.remove(force=True)
        client.close()


def test_approved_proposal_starts_and_stops_through_the_full_service(
    tmp_path: Path,
) -> None:
    source = tmp_path / "static-project"
    source.mkdir()
    (source / "index.html").write_text("workflow-ready", encoding="utf-8")
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    client = docker.from_env()
    sandbox_id = uuid4().hex
    project_name = f"workflow-sandbox-{sandbox_id[:8]}"
    project_volume = client.volumes.create(
        name=f"orchestrator-workflow-test-{sandbox_id[:12]}",
        labels={
            PROJECT_MANAGED: "true",
            PROJECT_NAME: project_name,
            LABEL_SOURCE: str(source),
            LABEL_CREATED_AT: "2026-08-04T00:00:00Z",
            LABEL_COPY_MODE: "snapshot",
            LABEL_FILE_COUNT: "1",
            LABEL_COPIED_BYTES: "14",
            LABEL_EXCLUDED_DIRECTORIES: "",
            LABEL_COPY_IMAGE: "alpine:latest",
            LABEL_STATUS_STORAGE: STATUS_STORAGE_PROJECT_VOLUME,
            LABEL_COPY_JOB_ID: uuid4().hex,
            LABEL_PROJECT_ID: uuid4().hex,
            LABEL_SANDBOX_ID: sandbox_id,
        },
    )
    run = None
    try:
        client.containers.run(
            "alpine:latest",
            [
                "sh",
                "-c",
                (
                    "set -eu; mkdir -p /project/.orchestrator/copy-job; "
                    "printf workflow-ready > /project/index.html; "
                    "printf completed > /project/.orchestrator/copy-job/status; "
                    "printf 2026-08-04T00:00:00Z > /project/.orchestrator/copy-job/started_at; "
                    "printf 2026-08-04T00:00:01Z > /project/.orchestrator/copy-job/finished_at; "
                    "printf 0 > /project/.orchestrator/copy-job/exit_code; "
                    ": > /project/.orchestrator/copy-job/error; "
                    ": > /project/.orchestrator/copy-job/copy.log"
                ),
            ],
            remove=True,
            volumes={project_volume.name: {"bind": "/project", "mode": "rw"}},
        )
        settings = PreviewSettings(
            inspection_image="alpine:latest",
            default_expiry_minutes=30,
            maximum_file_bytes=1_048_576,
            maximum_snapshot_bytes=16_777_216,
            proposal_lifetime_seconds=900,
            prepare_timeout_seconds=600,
            build_timeout_seconds=900,
        )
        proposal = propose_preview(client, store, settings, project_name)
        port = _available_host_port()
        response_command = (
            "sh -c 'while true; do printf \"HTTP/1.1 200 OK\\r\\n"
            "Content-Length: 14\\r\\n\\r\\nworkflow-ready\" | "
            "nc -l -p 8000; echo backend-request >&2; done'"
        )
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="alpine:latest",
            start_command=response_command,
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
            ),
        )

        with urllib.request.urlopen(run.url, timeout=3) as response:
            assert response.read().decode() == "workflow-ready"
        progress = preview_creation_logs(
            client,
            store,
            project_name,
            proposal.id,
        )
        assert progress.preview_id == run.id
        assert progress.status == "running"
        assert progress.events[0].step == "approved"
        assert progress.events[-1].step == "ready"
        runtime_logs = preview_logs(
            client,
            store,
            settings,
            project_name,
        )
        assert "backend-request" in "\n".join(runtime_logs.logs.values())
        stopped = stop_preview(
            client,
            store,
            project_name,
            remove_data_volumes=True,
        )
        assert stopped.stopped is True
        run = None
    finally:
        if run is not None:
            try:
                stop_preview(
                    client,
                    store,
                    project_name,
                    remove_data_volumes=True,
                )
            except Exception:
                pass
        project_volume.remove(force=True)
        client.close()


def test_native_preview_reports_real_container_and_dependency_durations() -> None:
    """Phase 5: the `container` and `dependencies` steps carry real timing.

    The app image (`alpine:latest`) bakes in no `HEALTHCHECK`, so this also
    exercises the `_wait_for_container_health` branch that treats `running`
    as the first successful probe once Docker reports no `Health` status at
    all.
    """
    client = docker.from_env()
    run_id = uuid4().hex
    project_volume = client.volumes.create(
        name=f"orchestrator-preview-timing-{run_id[:12]}"
    )
    resources: dict[str, Any] = {"containers": [], "networks": [], "volumes": []}
    events: list[tuple[str, str, int | None, str | None]] = []

    def progress(
        step: str,
        message: str,
        duration_ms: int | None = None,
        started_at: str | None = None,
    ) -> None:
        events.append((step, message, duration_ms, started_at))

    try:
        port = _available_host_port()
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.VITE,
            image="alpine:latest",
            # No package registry access needed: the sleep stands in for
            # real install work so the measured duration is real but the
            # test stays fast and network-free.
            install_command="mkdir -p node_modules && sleep 0.3",
            start_command=(
                "sh -c 'while true; do printf \"HTTP/1.1 200 OK\\r\\n"
                "Content-Length: 12\\r\\n\\r\\ntiming-ready\" | "
                "nc -l -p 8000; done'"
            ),
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        settings = _settings()
        labels = _labels("timing-sandbox", run_id, "2099-01-01T00:00:00Z")

        resources = _start_native(
            client,
            settings,
            project_volume.name,
            config,
            labels,
            run_id,
            port,
            progress=progress,
        )

        body = ""
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/",
                    timeout=1,
                ) as response:
                    body = response.read().decode()
                break
            except OSError:
                time.sleep(0.1)
        assert body == "timing-ready"

        container_events = [event for event in events if event[0] == "container"]
        assert len(container_events) == 2
        (start_step, start_message, start_duration, start_started_at) = container_events[0]
        (done_step, done_message, done_duration, done_started_at) = container_events[1]
        assert start_duration is None
        assert start_started_at is not None
        assert done_duration is not None and done_duration >= 0
        assert done_started_at == start_started_at
        assert done_message == "Application container started"

        dependency_events = [event for event in events if event[0] == "dependencies"]
        assert len(dependency_events) == 2
        dep_start_duration = dependency_events[0][2]
        dep_done_duration = dependency_events[1][2]
        assert dep_start_duration is None
        # A real `sleep 0.3` inside the install container: the measured
        # duration must reflect that, not a hardcoded or estimated value.
        assert dep_done_duration is not None and dep_done_duration >= 250

        print(
            "measured durations (ms): "
            f"container={done_duration} dependencies={dep_done_duration}"
        )
    finally:
        _remove_resources(resources, remove_data_volumes=True)
        project_volume.remove(force=True)
        client.close()


def test_reused_dependency_volume_still_reports_zero_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test covers dependency reuse. Environment masking has separate
    # integration coverage and Docker Desktop cannot mount pytest's private
    # temporary directory into a container on macOS.
    monkeypatch.setattr("app.previews.service._environment_masks", lambda *_: [])
    client = docker.from_env()
    # `_dependency_volume_name` truncates to the first 12 characters of the
    # sandbox id, so the random part has to lead or two runs collide on the
    # same dependency volume name.
    sandbox_id = uuid4().hex
    project_volume = client.volumes.create(
        name=f"orchestrator-preview-reuse-{uuid4().hex[:12]}"
    )
    first_resources: dict[str, Any] = {"containers": [], "networks": [], "volumes": []}
    second_resources: dict[str, Any] = {"containers": [], "networks": [], "volumes": []}
    dependency_volume_name = ""
    events: list[tuple[str, str, int | None, str | None]] = []

    def progress(
        step: str,
        message: str,
        duration_ms: int | None = None,
        started_at: str | None = None,
    ) -> None:
        events.append((step, message, duration_ms, started_at))

    try:
        settings = _settings()
        first_run_id = uuid4().hex
        first_config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.VITE,
            image="alpine:latest",
            install_command="mkdir -p node_modules && printf ready > node_modules/marker",
            start_command="sh -c 'while true; do sleep 1; done'",
            container_port=8000,
            host_port=_available_host_port(),
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )
        first_labels = _labels(sandbox_id, first_run_id, "2099-01-01T00:00:00Z")
        first_resources = _start_native(
            client,
            settings,
            project_volume.name,
            first_config,
            first_labels,
            first_run_id,
            first_config.host_port,
            progress=progress,
        )
        for volume in first_resources["volumes"]:
            if volume.name.startswith("orchestrator-deps-"):
                dependency_volume_name = volume.name
        assert dependency_volume_name

        second_run_id = uuid4().hex
        second_config = first_config.model_copy(
            update={"host_port": _available_host_port()}
        )
        second_labels = _labels(sandbox_id, second_run_id, "2099-01-01T00:00:00Z")
        second_resources = _start_native(
            client,
            settings,
            project_volume.name,
            second_config,
            second_labels,
            second_run_id,
            second_config.host_port,
            progress=progress,
        )

        dependency_messages = [
            event for event in events if event[0] == "dependencies"
        ]
        # First run: two events (running the install). Second run: one event
        # (skipped, zero duration) because the dependency volume from the
        # first run already has content under the same lockfile digest.
        assert len(dependency_messages) == 3
        reused_step, reused_message, reused_duration, reused_started_at = (
            dependency_messages[-1]
        )
        assert "skipping install" in reused_message
        assert reused_duration == 0
        assert reused_started_at is None
    finally:
        _remove_resources(first_resources, remove_data_volumes=True)
        _remove_resources(second_resources, remove_data_volumes=True)
        if dependency_volume_name:
            try:
                client.volumes.get(dependency_volume_name).remove(force=True)
            except docker.errors.NotFound:
                pass
        project_volume.remove(force=True)
        client.close()


def test_events_websocket_replays_and_streams_live_container_logs() -> None:
    """Phase 5 streaming: a client connecting mid-run gets the events already
    recorded, then keeps receiving new progress events and live container
    log lines, and disconnecting cleans up without leaving anything running
    behind that the final `stop_preview` cleanup does not already handle."""
    real_docker_client = docker.from_env()
    app.dependency_overrides[get_docker_client] = lambda: real_docker_client
    web_client = TestClient(app)
    store = get_controller_store()
    sandbox_id = uuid4().hex
    project_name = f"events-ws-sandbox-{sandbox_id[:8]}"
    project_volume = real_docker_client.volumes.create(
        name=f"orchestrator-events-ws-test-{sandbox_id[:12]}",
        labels={
            PROJECT_MANAGED: "true",
            PROJECT_NAME: project_name,
            LABEL_SOURCE: f"/projects/{project_name}",
            LABEL_CREATED_AT: "2026-08-06T00:00:00Z",
            LABEL_COPY_MODE: "snapshot",
            LABEL_FILE_COUNT: "0",
            LABEL_COPIED_BYTES: "0",
            LABEL_EXCLUDED_DIRECTORIES: "",
            LABEL_COPY_IMAGE: "alpine:latest",
            LABEL_STATUS_STORAGE: STATUS_STORAGE_PROJECT_VOLUME,
            LABEL_COPY_JOB_ID: uuid4().hex,
            LABEL_PROJECT_ID: uuid4().hex,
            LABEL_SANDBOX_ID: sandbox_id,
        },
    )
    run: Any = None
    try:
        real_docker_client.containers.run(
            "alpine:latest",
            [
                "sh",
                "-c",
                (
                    "set -eu; mkdir -p /project/.orchestrator/copy-job; "
                    "printf completed > /project/.orchestrator/copy-job/status; "
                    "printf 2026-08-06T00:00:00Z > /project/.orchestrator/copy-job/started_at; "
                    "printf 2026-08-06T00:00:01Z > /project/.orchestrator/copy-job/finished_at; "
                    "printf 0 > /project/.orchestrator/copy-job/exit_code; "
                    ": > /project/.orchestrator/copy-job/error; "
                    ": > /project/.orchestrator/copy-job/copy.log"
                ),
            ],
            remove=True,
            volumes={project_volume.name: {"bind": "/project", "mode": "rw"}},
        )
        settings = _settings()
        proposal = propose_preview(real_docker_client, store, settings, project_name)
        port = _available_host_port()
        config = PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=PreviewRuntime.UNKNOWN,
            image="alpine:latest",
            # Ticks continuously to stdout so the attach stream always has
            # something to forward once the app container starts.
            start_command=(
                "sh -c 'i=0; while true; do echo tick-$i; i=$((i+1)); sleep 0.2; done'"
            ),
            container_port=8000,
            host_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=30,
        )

        result: dict[str, Any] = {}

        def run_start_preview() -> None:
            result["run"] = start_preview(
                real_docker_client,
                store,
                settings,
                project_name,
                StartPreviewRequest(
                    proposal_id=proposal.id,
                    proposal_digest=proposal.digest,
                    config=config,
                    action=PreviewAction.START,
                    actor="integration-test",
                ),
            )

        starter = threading.Thread(target=run_start_preview)
        starter.start()
        # Give `start_preview` time to record its early "approved" event
        # before the websocket connects, so replay has something to replay.
        time.sleep(0.3)

        seen_types: set[str] = set()
        saw_replayed_approved = False
        log_count = 0
        # Disconnects after a couple of live log lines rather than waiting
        # for the preview to finish: that exercises a genuine mid-stream
        # disconnect (the server is almost certainly still inside its
        # polling loop, not in the middle of its own natural teardown), so
        # this does not race the server's own terminal-status close.
        with web_client.websocket_connect(
            f"/projects/{project_name}/preview-proposals/{proposal.id}/events"
        ) as websocket:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and log_count < 2:
                message = websocket.receive_json()
                seen_types.add(message["type"])
                if message["type"] == "progress" and message["step"] == "approved":
                    saw_replayed_approved = True
                if message["type"] == "log":
                    log_count += 1

        starter.join(timeout=15)
        run = result.get("run")

        from app.previews.router import _active_event_sessions

        assert run is not None and run.status == "running"
        assert saw_replayed_approved
        assert log_count >= 2
        # The client's own `with` block exiting only sends the close frame;
        # the server-side handler's teardown (cancelling log tasks, closing
        # attach sockets, clearing the session) runs asynchronously after
        # that, so give it a moment to actually finish.
        session_key = f"{project_name}:{proposal.id}"
        for _ in range(50):
            if session_key not in _active_event_sessions:
                break
            time.sleep(0.1)
        assert session_key not in _active_event_sessions
    finally:
        if run is not None:
            try:
                stop_preview(
                    real_docker_client,
                    store,
                    project_name,
                    remove_data_volumes=True,
                )
            except Exception:
                pass
        app.dependency_overrides.clear()
        project_volume.remove(force=True)
        real_docker_client.close()
