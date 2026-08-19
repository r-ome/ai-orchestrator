import io
import json
import re
import secrets
import shlex
import socket
import time
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any
from uuid import uuid4

import yaml
from docker.client import DockerClient
from docker.errors import (
    APIError,
    BuildError,
    DockerException,
)
from docker.models.containers import Container
from docker.types import Mount
from requests.exceptions import ReadTimeout

from app.containers.hardened import (
    Egress,
    HardenedContainerSpec,
    Rootfs,
    create_hardened,
)
from app.controller.config import get_controller_settings
from app.controller.store import ControllerStore, SandboxWriterAdmissionError
from app.dependency_cache import (
    _DEPENDENCY_READY_MARKER,
    _data_volume,
    _dependency_volume,
    _dependency_volume_ready,
    _lockfile_digest,
    _run_volume_name,
    _volume_context_tar,
    _volume_environment_files,
    _volume_runtime_files,
    _write_preview_manifest,
)
from app.platform.labels import (
    LABEL_EXPIRES_AT,
    LABEL_MANAGED,
    LABEL_RUN_ID,
    LABEL_SANDBOX_ID,
    LABEL_SERVICE,
)
from app.previews.config import PreviewSettings
from app.previews._shared import (
    _expiry,
    _now,
    _project_key,
    _ready_project,
    _safe_relative_path,
    _slug,
    _time_after,
)
from app.previews.detection import (
    capture_source_runtime_files,
    compare_files,
    detect_preview,
    hashes,
    parse_environment_names,
    proposal_digest,
)
from app.previews.errors import PreviewOperationError
from app.previews.health import _wait_for_container_health, _wait_for_mysql_health
from app.previews.models import (
    PreviewAction,
    PreviewConfiguration,
    PreviewContainer,
    PreviewDependencyService,
    PreviewEnvironmentSource,
    PreviewKind,
    PreviewLogs,
    PreviewMode,
    PreviewNetworkAccess,
    PreviewPersistence,
    PreviewProgressEvent,
    PreviewProposal,
    PreviewRun,
    PreviewRuntime,
    PreviewSharing,
    StartPreviewRequest,
    StopPreviewResponse,
)
from app.previews.progress import (
    ProgressReporter,
    _ignore_progress,
    _record_preview_progress,
    _timed_step,
)
from app.previews.runtimes.compose import _start_compose
from app.previews.runtimes.dockerfile import _start_dockerfile
from app.previews.runtimes.native import _COMMIT_PATTERN, _export_commit, _start_native
from app.previews.resources import (
    PREVIEW_COMMAND_MAX_LOG_BYTES,
    PREVIEW_COMMAND_TIMEOUT_SECONDS,
    _disconnect_foreign_endpoints,
    _ensure_preview_image,
    _existing_volume,
    _labels,
    _preview_containers,
    _preview_images,
    _preview_networks,
    _preview_volumes,
    _remove_resources,
    _resources_for_run,
    _run_preview_command,
    _validate_built_image,
)
from app.previews.sharing import (
    _attach_shared_database,
    _connect_sandbox_database_endpoint,
    _database_engine,
    _managed_preview_database,
    _release_shared_database,
    _restart_shared_database,
    _sharing_state,
    _validate_sharing,
)
from app.sandboxes.git import run_git
from app.sandboxes.database import (
    DatabaseConnectionRequest,
    DatabaseMigrationRequest,
    DatabaseProvisionRequest,
    SandboxDatabaseError,
    SandboxDatabaseRuntime,
    sandbox_database_runtime,
)
from app.tasks.models import TaskStatus
from app.tasks.service import transition_task


SHARED_DATABASE_PREFIX = "orchestrator-shared-db-"
_preview_lock = Lock()


def propose_preview(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
) -> PreviewProposal:
    project = _ready_project(docker_client, project_name, controller_store)
    files = _volume_runtime_files(
        docker_client,
        project.volume_name,
        settings,
    )
    baseline = controller_store.latest_baseline(project.sandbox_id)
    if not baseline:
        baseline_files = _original_baseline(project, settings) or files
        controller_store.record_initial_baseline(
            project.sandbox_id,
            baseline_files,
            hashes(baseline_files),
        )
        baseline = controller_store.latest_baseline(project.sandbox_id)

    environment_files = _volume_environment_files(
        docker_client,
        project.volume_name,
        settings,
    )
    environment_names = parse_environment_names(environment_files)
    detection = detect_preview(
        files,
        default_expiry_minutes=settings.default_expiry_minutes,
        environment_names=environment_names,
    )
    project_key = _project_key(project)
    secret_names = set(controller_store.project_secret_names(project_key))
    controller_managed = {
        variable
        for variable, source in detection.config.environment.items()
        if source.from_service
    }
    for name in detection.required_environment:
        if name in controller_managed or name not in secret_names:
            continue
        detection.config.environment[name] = PreviewEnvironmentSource(from_secret=name)
    configured_environment, missing_environment = _environment_status(
        detection.required_environment,
        secret_names,
        controller_managed,
    )
    protected_hashes = hashes(files)
    changes = compare_files(files, baseline)
    created_at = _now()
    expires_at = _time_after(seconds=settings.proposal_lifetime_seconds)
    digest = proposal_digest(detection.config, protected_hashes)
    previous_approval = controller_store.latest_approval(project.sandbox_id)
    approval_required = (
        previous_approval is None
        or previous_approval.get("proposal_digest") != digest
        or bool(changes)
    )
    proposal_id = uuid4().hex
    controller_store.create_review(
        review_id=proposal_id,
        sandbox_id=project.sandbox_id,
        proposal_digest=digest,
        detected_mode=detection.mode.value,
        config=detection.config.model_dump(mode="json"),
        protected_files=protected_hashes,
        changes=[change.model_dump() for change in changes],
        created_at=created_at,
        expires_at=expires_at,
    )
    return PreviewProposal(
        id=proposal_id,
        digest=digest,
        sandbox_id=project.sandbox_id,
        project_name=project.name,
        detected_mode=detection.mode,
        detected_runtime=detection.runtime,
        confidence=detection.confidence,
        evidence=detection.evidence,
        available_services=detection.available_services,
        config=detection.config,
        protected_files=protected_hashes,
        changes=changes,
        approval_required=approval_required,
        created_at=created_at,
        expires_at=expires_at,
        required_environment=detection.required_environment,
        missing_environment=missing_environment,
        configured_environment=configured_environment,
    )


def _environment_status(
    required_environment: list[str],
    secret_names: set[str],
    controller_managed: set[str],
) -> tuple[list[str], list[str]]:
    configured = sorted(secret_names | controller_managed)
    missing = sorted(set(required_environment) - set(configured))
    return configured, missing


def _preview_target(
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    task_id: str,
) -> tuple[PreviewKind, str, str]:
    """Decides which preview kind is being started, and from which commit.

    The commit comes from the task row the controller wrote after reading the
    task branch itself, never from the request and never from a file in the
    sandbox.
    """
    if not task_id:
        return PreviewKind.LIVE, "", ""
    row = controller_store.task(task_id)
    if row is None or str(row.get("sandbox_id")) != sandbox_id:
        raise PreviewOperationError(404, f"Task '{task_id}' is unknown")
    status = str(row.get("status") or "")
    if status not in {
        TaskStatus.REPORTED.value,
        TaskStatus.PREVIEWING.value,
        TaskStatus.REVIEW.value,
    }:
        raise PreviewOperationError(
            409,
            f"Task '{task_id}' has no reviewable commit (status '{status}')",
        )
    commit_sha = str(row.get("head_commit") or "")
    if not _COMMIT_PATTERN.match(commit_sha):
        raise PreviewOperationError(409, f"Task '{task_id}' has no head commit")
    _move_task(controller_store, task_id, TaskStatus.PREVIEWING)
    return PreviewKind.TASK, task_id, commit_sha


def _move_task(
    controller_store: ControllerStore,
    task_id: str,
    to_status: TaskStatus,
) -> None:
    """Best-effort status move. A refused transition never fails a preview."""
    if not task_id:
        return
    transition_task(controller_store, task_id=task_id, to_status=to_status)


def start_preview(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
    request: StartPreviewRequest,
) -> PreviewRun:
    project = _ready_project(docker_client, project_name, controller_store)
    with _preview_lock:
        active = controller_store.active_preview(project.sandbox_id)
        if active is not None:
            if request.action is PreviewAction.REUSE:
                return _run_from_record(
                    docker_client,
                    project.name,
                    active,
                    controller_store,
                )
            if request.action is PreviewAction.RESTART:
                return restart_preview(
                    docker_client,
                    controller_store,
                    settings,
                    project_name,
                )
            if request.action is PreviewAction.REBUILD:
                stop_preview(
                    docker_client,
                    controller_store,
                    project_name,
                    remove_data_volumes=True,
                    status="stopped",
                )
            else:
                raise PreviewOperationError(
                    409,
                    "Sandbox already has an active preview; choose reuse, restart, or rebuild",
                )

        review = controller_store.review(request.proposal_id)
        if review is None or review.get("sandbox_id") != project.sandbox_id:
            raise PreviewOperationError(404, "Preview proposal was not found")
        if review.get("proposal_digest") != request.proposal_digest:
            raise PreviewOperationError(409, "Preview proposal digest does not match")
        if str(review.get("expires_at", "")) <= _now():
            raise PreviewOperationError(409, "Preview proposal expired; inspect again")

        kind, task_id, commit_sha = _preview_target(
            controller_store,
            sandbox_id=project.sandbox_id,
            task_id=request.task_id,
        )
        if kind is PreviewKind.TASK and request.config.mode is not PreviewMode.NATIVE:
            raise PreviewOperationError(
                422,
                "Task previews run only in native mode",
            )

        run_id = uuid4().hex
        host_port = request.config.host_port or _available_host_port()
        created_at = _now()
        expires_at = _expiry(request.config.expiry_minutes)
        labels = _labels(project.sandbox_id, run_id, expires_at)
        values = {
            "id": run_id,
            "sandbox_id": project.sandbox_id,
            "proposal_id": request.proposal_id,
            "mode": request.config.mode.value,
            "kind": kind.value,
            "task_id": task_id or None,
            "commit_sha": commit_sha or None,
            "status": "preparing",
            "selected_service": request.config.selected_service,
            "container_port": request.config.container_port,
            "host_port": host_port,
            "config_json": json.dumps(
                request.config.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            # The approved digest is filled after the controller re-reads the
            # runtime files. The proposal digest is a non-null preparation
            # sentinel and remains controller-owned.
            "config_digest": request.proposal_digest,
            "network_name": None,
            "created_at": created_at,
            "started_at": None,
            "expires_at": expires_at,
            "last_activity_at": created_at,
        }
        try:
            # This row must exist before even an inspection or export helper
            # can create a throwaway Docker container.
            controller_store.create_preview_run(values)
        except SandboxWriterAdmissionError as error:
            if kind is PreviewKind.TASK:
                _move_task(controller_store, task_id, TaskStatus.REVIEW)
            raise PreviewOperationError(409, str(error)) from error
        except Exception:
            if kind is PreviewKind.TASK:
                _move_task(controller_store, task_id, TaskStatus.REVIEW)
            raise

        progress = (
            lambda step, message, duration_ms=None, started_at=None: _record_preview_progress(
                controller_store,
                sandbox_id=project.sandbox_id,
                proposal_id=request.proposal_id,
                preview_id=run_id,
                status="preparing",
                step=step,
                message=message,
                duration_ms=duration_ms,
                started_at=started_at,
            )
        )
        resources: dict[str, Any] = {
            "containers": [],
            "networks": [],
            "volumes": [],
            "images": [],
        }
        try:
            files = _volume_runtime_files(
                docker_client,
                project.volume_name,
                settings,
            )
            current_hashes = hashes(files)
            proposed_hashes = json.loads(
                str(review.get("protected_files_json") or "{}")
            )
            if current_hashes != proposed_hashes:
                raise PreviewOperationError(
                    409,
                    "Protected runtime files changed after inspection; inspect again",
                )
            if request.save_default:
                _write_preview_manifest(
                    docker_client,
                    project.volume_name,
                    settings.inspection_image,
                    request.config,
                )
                files = _volume_runtime_files(
                    docker_client,
                    project.volume_name,
                    settings,
                )
                current_hashes = hashes(files)
            # A task preview's approval points at a commit, not at a hash map:
            # the exported tree cannot drift after approval.
            approved_digest = (
                proposal_digest(request.config, {"commit": commit_sha})
                if kind is PreviewKind.TASK
                else proposal_digest(request.config, current_hashes)
            )
            _validate_sharing(request.config)
            controller_store.approve_review(
                review_id=request.proposal_id,
                sandbox_id=project.sandbox_id,
                proposal_digest=approved_digest,
                config=request.config.model_dump(mode="json"),
                actor=request.actor,
                files=files,
                hashes=current_hashes,
            )
            progress(
                "approved",
                f"Approved {request.config.mode.value} preview settings; assigned host port {host_port}",
            )
            if request.config.mode is PreviewMode.NATIVE:
                resources = _start_native(
                    docker_client,
                    settings,
                    project.volume_name,
                    request.config,
                    labels,
                    run_id,
                    host_port,
                    expected_protected_hashes=current_hashes,
                    progress=progress,
                    controller_store=controller_store,
                    project_key=_project_key(project),
                    source_path=project.source_path,
                    secrets=controller_store.project_secrets(
                        _project_key(project)
                    ),
                    kind=kind,
                    commit_sha=commit_sha,
                )
            elif request.config.mode is PreviewMode.DOCKERFILE:
                resources = _start_dockerfile(
                    docker_client,
                    settings,
                    project.volume_name,
                    request.config,
                    labels,
                    run_id,
                    host_port,
                    progress=progress,
                    secrets=controller_store.project_secrets(
                        _project_key(project)
                    ),
                    controller_store=controller_store,
                )
            elif request.config.mode is PreviewMode.COMPOSE:
                resources = _start_compose(
                    docker_client,
                    settings,
                    project.volume_name,
                    files,
                    request.config,
                    labels,
                    run_id,
                    host_port,
                    progress=progress,
                    secrets=controller_store.project_secrets(
                        _project_key(project)
                    ),
                    controller_store=controller_store,
                )
            else:
                raise PreviewOperationError(
                    422,
                    "Unknown projects require an approved native, Dockerfile, or Compose configuration",
                )
        except Exception as error:
            try:
                resources = _resources_for_run(docker_client, run_id)
            except DockerException:
                pass
            _remove_resources(resources, remove_data_volumes=True)
            controller_store.update_preview_run(
                run_id,
                status="failed",
                stopped_at=_now(),
            )
            if kind is PreviewKind.TASK:
                _move_task(controller_store, task_id, TaskStatus.REVIEW)
            _record_preview_progress(
                controller_store,
                sandbox_id=project.sandbox_id,
                proposal_id=request.proposal_id,
                preview_id=run_id,
                status="failed",
                step="failed",
                message=f"Preview creation failed: {error}",
                level="error",
            )
            raise

        network_name = ",".join(
            network.name for network in resources.get("networks") or []
        )
        try:
            controller_store.update_preview_run(
                run_id,
                status="running",
                config_digest=approved_digest,
                network_name=network_name,
                started_at=_now(),
                last_activity_at=_now(),
            )
        except Exception as error:
            _remove_resources(resources, remove_data_volumes=True)
            try:
                controller_store.update_preview_run(
                    run_id,
                    status="failed",
                    stopped_at=_now(),
                )
            except Exception:
                pass
            if kind is PreviewKind.TASK:
                _move_task(controller_store, task_id, TaskStatus.REVIEW)
            _record_preview_progress(
                controller_store,
                sandbox_id=project.sandbox_id,
                proposal_id=request.proposal_id,
                preview_id=run_id,
                status="failed",
                step="record",
                message=f"Preview containers started, but controller state failed: {error}",
                level="error",
            )
            raise
        _record_preview_progress(
            controller_store,
            sandbox_id=project.sandbox_id,
            proposal_id=request.proposal_id,
            preview_id=run_id,
            status="running",
            step="ready",
            message=f"Preview is running at http://127.0.0.1:{host_port}",
        )
        if kind is PreviewKind.TASK:
            _move_task(controller_store, task_id, TaskStatus.REVIEW)
        record = controller_store.preview_run(run_id)
        if record is None:
            raise PreviewOperationError(500, "Preview state was not recorded")
        return _run_from_record(docker_client, project.name, record, controller_store)


def get_current_preview(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    *,
    touch: bool = False,
    expiry_minutes: int | None = None,
) -> PreviewRun:
    project = _ready_project(docker_client, project_name, controller_store)
    record = controller_store.active_preview(project.sandbox_id)
    if record is None:
        raise PreviewOperationError(404, "Sandbox has no active preview")
    if touch:
        if expiry_minutes is None:
            config = PreviewConfiguration.model_validate_json(str(record["config_json"]))
            expiry_minutes = config.expiry_minutes
        controller_store.touch_preview(
            str(record["id"]),
            expires_at=_expiry(expiry_minutes),
        )
        record = controller_store.preview_run(str(record["id"])) or record
    return _run_from_record(docker_client, project.name, record, controller_store)


def reuse_preview(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
) -> PreviewRun:
    return get_current_preview(
        docker_client,
        controller_store,
        project_name,
        touch=True,
        expiry_minutes=None,
    )


def restart_preview(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
) -> PreviewRun:
    project = _ready_project(docker_client, project_name, controller_store)
    record = controller_store.active_preview(project.sandbox_id)
    if record is None:
        raise PreviewOperationError(404, "Sandbox has no active preview")
    config = PreviewConfiguration.model_validate_json(str(record["config_json"]))
    kind = PreviewKind(str(record.get("kind") or PreviewKind.LIVE.value))
    commit_sha = str(record.get("commit_sha") or "")
    if kind is PreviewKind.TASK:
        if proposal_digest(config, {"commit": commit_sha}) != record.get("config_digest"):
            raise PreviewOperationError(
                409,
                "Task preview approval does not match its commit; rebuild it",
            )
    else:
        files = _volume_runtime_files(docker_client, project.volume_name, settings)
        if proposal_digest(config, hashes(files)) != record.get("config_digest"):
            raise PreviewOperationError(
                409,
                "Protected runtime files changed; inspect and approve a rebuild",
            )
    containers = _preview_containers(docker_client, str(record["id"]), all=True)
    if not containers:
        controller_store.update_preview_run(str(record["id"]), status="missing")
        raise PreviewOperationError(409, "Preview containers are missing; rebuild it")
    controller_store.update_preview_run(str(record["id"]), status="restarting")
    try:
        if kind is PreviewKind.TASK:
            # Containers restart against the same workspace volume, so the
            # commit has to be re-exported first. Without this a restart serves
            # whatever the first export left behind, which is the bug this
            # phase fixes.
            _export_commit(
                docker_client,
                settings.git_image,
                project.volume_name,
                _run_volume_name(str(record["id"]), "runtime-workspace"),
                commit_sha,
            )
        database = config.services.get("database")
        managed_database = _managed_preview_database(
            docker_client,
            controller_store,
            project.sandbox_id,
        )
        shared = (
            database is not None
            and database.sharing is not PreviewSharing.ISOLATED
        )
        if managed_database is not None:
            for container in containers:
                container.restart(timeout=5)
        elif config.mode is PreviewMode.NATIVE and database is not None and shared:
            by_service = {
                _container_service(container): container for container in containers
            }
            application_container = by_service.get("app")
            if application_container is None:
                raise PreviewOperationError(
                    409,
                    "The preview application container is missing; rebuild it",
                )
            database_container = _restart_shared_database(
                docker_client,
                settings,
                project_key=_project_key(project),
                source_path=project.source_path,
                database=database,
                run_id=str(record["id"]),
            )
            _wait_for_mysql_health(
                database_container,
                timeout_seconds=settings.prepare_timeout_seconds,
            )
            application_container.restart(timeout=5)
            gateway = by_service.get("gateway")
            if gateway is not None:
                gateway.restart(timeout=5)
        elif config.mode is PreviewMode.NATIVE and database is not None:
            by_service = {
                _container_service(container): container for container in containers
            }
            database_container = by_service.get("database")
            application_container = by_service.get("app")
            if database_container is None or application_container is None:
                raise PreviewOperationError(
                    409,
                    "Native database preview containers are missing; rebuild it",
                )
            database_container.reload()
            health = (
                (database_container.attrs.get("State") or {})
                .get("Health", {})
                .get("Status")
            )
            if database_container.status != "running":
                database_container.start()
            elif health == "unhealthy":
                database_container.restart(timeout=5)
            _wait_for_mysql_health(
                database_container,
                timeout_seconds=settings.prepare_timeout_seconds,
            )
            application_container.restart(timeout=5)
            gateway = by_service.get("gateway")
            if gateway is not None:
                gateway.restart(timeout=5)
        else:
            for container in containers:
                container.restart(timeout=5)
    except DockerException:
        controller_store.update_preview_run(str(record["id"]), status="failed")
        raise
    except PreviewOperationError:
        controller_store.update_preview_run(str(record["id"]), status="failed")
        raise
    expires_at = _expiry(config.expiry_minutes)
    controller_store.update_preview_run(
        str(record["id"]),
        status="running",
        last_activity_at=_now(),
        expires_at=expires_at,
    )
    refreshed = controller_store.preview_run(str(record["id"])) or record
    return _run_from_record(docker_client, project.name, refreshed, controller_store)


def stop_preview(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    *,
    remove_data_volumes: bool,
    status: str = "stopped",
) -> StopPreviewResponse:
    project = _ready_project(docker_client, project_name, controller_store)
    record = controller_store.active_preview(project.sandbox_id)
    if record is None:
        raise PreviewOperationError(404, "Sandbox has no active preview")
    run_id = str(record["id"])
    controller_store.update_preview_run(run_id, status="stopping")
    resources = _resources_for_run(docker_client, run_id)
    counts = _remove_resources(resources, remove_data_volumes=remove_data_volumes)
    controller_store.update_preview_run(
        run_id,
        status=status,
        stopped_at=_now(),
    )
    # Released after the run leaves the active set, so the idle check that
    # decides whether to remove the shared server does not count this run.
    released = _release_shared_database(
        docker_client,
        controller_store,
        sandbox_id=project.sandbox_id,
    )
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=run_id,
        kind=f"preview.{status}",
        payload={**counts, "shared_database": released},
    )
    return StopPreviewResponse(
        id=run_id,
        stopped=True,
        removed_containers=counts["containers"],
        removed_networks=counts["networks"],
        removed_volumes=counts["volumes"],
        removed_images=counts["images"],
    )


def preview_logs(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
) -> PreviewLogs:
    run = reuse_preview(docker_client, controller_store, settings, project_name)
    return _preview_log_response(
        docker_client,
        controller_store,
        proposal_id=run.proposal_id,
        preview_id=run.id,
        fallback_status=run.status,
    )


def require_preview_proposal(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    proposal_id: str,
) -> dict[str, Any]:
    """Confirms `proposal_id` belongs to `project_name`'s sandbox.

    Shared by the polling logs endpoint and the events websocket, so both
    reject a proposal from a different sandbox the same way.
    """
    project = _ready_project(docker_client, project_name, controller_store)
    review = controller_store.review(proposal_id)
    if review is None or review.get("sandbox_id") != project.sandbox_id:
        raise PreviewOperationError(404, "Preview proposal was not found")
    return review


def preview_running_containers(docker_client: DockerClient, preview_id: str) -> list[Container]:
    return _preview_containers(docker_client, preview_id, all=False)


def open_preview_log_stream(docker_client: DockerClient, container: Container) -> Any:
    """Opens a raw, following attach stream for one preview container.

    Docker multiplexes stdout and stderr into this stream, the same as any
    other non-interactive attach; `read_stream` in `docker_terminal` pulls
    bytes off it exactly as it does for an exec socket.
    """
    return docker_client.api.attach_socket(
        container.id,
        params={"logs": "1", "stream": "1", "stdout": "1", "stderr": "1"},
    )


def preview_creation_logs(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    proposal_id: str,
) -> PreviewLogs:
    require_preview_proposal(docker_client, controller_store, project_name, proposal_id)
    events = controller_store.events_for_run(
        proposal_id,
        kind="preview.progress",
    )
    preview_id = ""
    status = "waiting"
    if events:
        payload = events[-1].get("payload") or {}
        preview_id = str(payload.get("preview_id") or "")
        status = str(payload.get("status") or "preparing")
    return _preview_log_response(
        docker_client,
        controller_store,
        proposal_id=proposal_id,
        preview_id=preview_id,
        fallback_status=status,
    )


def _preview_log_response(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    proposal_id: str,
    preview_id: str,
    fallback_status: str,
) -> PreviewLogs:
    logs: dict[str, str] = {}
    containers = (
        _preview_containers(docker_client, preview_id, all=True)
        if preview_id
        else []
    )
    for container in containers:
        try:
            output = container.logs(
                stdout=True,
                stderr=True,
                tail=200,
                timestamps=True,
            )
        except DockerException as error:
            logs[container.name] = f"Logs unavailable: {error}"
            continue
        if isinstance(output, bytes):
            logs[container.name] = output.decode("utf-8", errors="replace")[-65_536:]
        else:
            logs[container.name] = str(output)[-65_536:]
    stored_events = controller_store.events_for_run(
        proposal_id,
        kind="preview.progress",
    )
    events = []
    status = fallback_status
    for stored in stored_events:
        payload = stored.get("payload") or {}
        status = str(payload.get("status") or status)
        events.append(
            PreviewProgressEvent(
                id=int(stored["id"]),
                level=str(payload.get("level") or "info"),
                step=str(payload.get("step") or "preview"),
                message=str(payload.get("message") or ""),
                created_at=str(stored["created_at"]),
                started_at=payload.get("started_at"),
                duration_ms=payload.get("duration_ms"),
            )
        )
    return PreviewLogs(
        proposal_id=proposal_id,
        preview_id=preview_id,
        status=status,
        events=events,
        logs=logs,
    )


def expire_previews(
    docker_client: DockerClient,
    controller_store: ControllerStore,
) -> int:
    expired = controller_store.expired_previews(_now())
    count = 0
    for record in expired:
        run_id = str(record["id"])
        resources = _resources_for_run(docker_client, run_id)
        _remove_resources(resources, remove_data_volumes=True)
        controller_store.update_preview_run(
            run_id,
            status="expired",
            stopped_at=_now(),
        )
        released = _release_shared_database(
            docker_client,
            controller_store,
            sandbox_id=str(record["sandbox_id"]),
        )
        controller_store.event(
            sandbox_id=str(record["sandbox_id"]),
            run_id=run_id,
            kind="preview.expired",
            payload={"shared_database": released},
        )
        count += 1
    return count
























































def _run_from_record(
    docker_client: DockerClient,
    project_name: str,
    record: dict[str, Any],
    controller_store: ControllerStore | None = None,
) -> PreviewRun:
    config = PreviewConfiguration.model_validate_json(str(record["config_json"]))
    containers = _preview_containers(docker_client, str(record["id"]), all=True)
    summaries = []
    for container in containers:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        summaries.append(
            PreviewContainer(
                id=container.id,
                name=container.name,
                service=labels.get(LABEL_SERVICE, "app"),
                status=container.status,
            )
        )
    host_port = record.get("host_port")
    return PreviewRun(
        id=str(record["id"]),
        sandbox_id=str(record["sandbox_id"]),
        project_name=project_name,
        proposal_id=str(record["proposal_id"]),
        mode=PreviewMode(str(record["mode"])),
        kind=PreviewKind(str(record.get("kind") or PreviewKind.LIVE.value)),
        task_id=str(record["task_id"]) if record.get("task_id") else None,
        commit_sha=str(record["commit_sha"]) if record.get("commit_sha") else None,
        runtime=config.runtime,
        status=str(record["status"]),
        selected_service=str(record.get("selected_service") or "app"),
        container_port=int(record["container_port"]),
        host_port=int(host_port) if host_port else None,
        url=f"http://127.0.0.1:{host_port}" if host_port else "",
        network_access=config.network_access,
        created_at=str(record["created_at"]),
        started_at=str(record.get("started_at") or ""),
        expires_at=str(record.get("expires_at") or ""),
        last_activity_at=str(record["last_activity_at"]),
        containers=summaries,
        database_sharing=(
            _sharing_state(controller_store, str(record["sandbox_id"]))
            if controller_store is not None
            else None
        ),
    )


def _container_service(container: Container) -> str:
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    return str(labels.get(LABEL_SERVICE) or "app")


def _original_baseline(project: Any, settings: PreviewSettings) -> dict[str, bytes]:
    try:
        return capture_source_runtime_files(
            Path(project.source_path),
            maximum_file_bytes=settings.maximum_file_bytes,
            maximum_snapshot_bytes=settings.maximum_snapshot_bytes,
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return {}


def _available_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])
