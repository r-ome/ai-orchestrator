import os
import json
import time
import urllib.request
from uuid import uuid4
from pathlib import Path

import docker
import pytest
import yaml

from app.previews.config import PreviewSettings
from app.previews.models import (
    PreviewAction,
    PreviewConfiguration,
    PreviewMode,
    PreviewNetworkAccess,
    PreviewRuntime,
    StartPreviewRequest,
)
from app.previews.service import (
    _available_host_port,
    _labels,
    _remove_resources,
    _start_compose,
    _start_dockerfile,
    _start_native,
    _volume_runtime_files,
    preview_creation_logs,
    preview_logs,
    propose_preview,
    start_preview,
    stop_preview,
)
from app.controller.store import ControllerStore
from app.projects.service import (
    LABEL_COPIED_BYTES,
    LABEL_COPY_IMAGE,
    LABEL_COPY_JOB_ID,
    LABEL_COPY_MODE,
    LABEL_CREATED_AT,
    LABEL_EXCLUDED_DIRECTORIES,
    LABEL_FILE_COUNT,
    LABEL_MANAGED as PROJECT_MANAGED,
    LABEL_NAME as PROJECT_NAME,
    LABEL_PROJECT_ID,
    LABEL_SANDBOX_ID,
    LABEL_SOURCE,
    LABEL_STATUS_STORAGE,
    STATUS_STORAGE_PROJECT_VOLUME,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
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
                        "test ! -e /workspace/.env",
                        "test ! -e /workspace/.env.local",
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
        assert application.exec_run(["test", "!", "-e", "/workspace/.env"]).exit_code == 0
        assert (
            application.exec_run(
                ["test", "!", "-e", "/workspace/.env.local"]
            ).exit_code
            == 0
        )
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
