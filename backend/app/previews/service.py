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
from app.labels import (
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


PREVIEW_CONTAINER_PREFIX = "orchestrator-preview-"
SHARED_DATABASE_PREFIX = "orchestrator-shared-db-"
# The env files a preview must never read. Same pair the copy-time exclusion
# used, so a live preview loses nothing the copied workspace already hid.
_MASKED_ENVIRONMENT_NAMES = (".env", ".env.local")
# Docker bind-mounts a character device onto the env paths, but Vite treats the
# device-backed paths as changing files and restarts forever. Use one stable,
# empty regular file instead. Docker creates the target file when it is absent,
# and the regular source keeps its inode metadata stable for file watchers.
_MASK_SOURCE_NAME = "preview-env-mask"
# Refuse to start rather than leave one env file unmasked.
_MAXIMUM_ENVIRONMENT_MASKS = 100
# Controller metadata a preview has no business reading. A directory, so tmpfs
# masks it.
_MASKED_DIRECTORIES = (".orchestrator",)
# Build output a live preview must write somewhere other than the sandbox
# worktree, or every completion report fails the dirty-tree check on artifacts
# the project happens not to gitignore.
_BUILD_OUTPUT_PATHS = ("dist", ".astro", ".next")
_NODE_RUNTIMES = {"astro", "vite", "nextjs"}
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
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


def _start_native(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
    host_port: int,
    expected_protected_hashes: dict[str, str] | None = None,
    progress: ProgressReporter | None = None,
    controller_store: ControllerStore | None = None,
    project_key: str = "",
    source_path: str = "",
    secrets: dict[str, str] | None = None,
    kind: PreviewKind = PreviewKind.LIVE,
    commit_sha: str = "",
) -> dict[str, Any]:
    report = progress or _ignore_progress
    report("image", f"Checking runtime image {config.image}")
    _ensure_preview_image(docker_client, config.image)
    database = config.services.get("database")
    data_volumes: list[Any] = []
    if kind is PreviewKind.TASK:
        with _timed_step(
            report, "workspace", f"Exporting sandbox commit {commit_sha[:12]}"
        ) as finish:
            _ensure_preview_image(docker_client, settings.git_image)
            workspace = _data_volume(
                docker_client,
                run_id,
                "runtime-workspace",
                labels,
                False,
            )
            data_volumes.append(workspace)
            _export_commit(
                docker_client,
                settings.git_image,
                project_volume,
                workspace.name,
                commit_sha,
            )
            workspace_volume = workspace.name
            finish("Runtime workspace holds the task commit")
    else:
        # The sandbox volume itself, so an agent's edit reaches the development
        # server without a copy and without a restart.
        report("workspace", "Mounting the sandbox for a live preview")
        _exclude_preview_masks(
            docker_client,
            settings.inspection_image,
            project_volume,
        )
        workspace_volume = project_volume
        report("workspace", "Sandbox workspace is ready")
    mounts = _environment_masks(docker_client, settings, workspace_volume)
    tmpfs = {
        "/tmp": "rw,nosuid,size=256m",
        **{
            f"/workspace/{path}": "rw,nosuid,size=1m"
            for path in _MASKED_DIRECTORIES
        },
    }
    volumes: dict[str, dict[str, str]] = {
        workspace_volume: {"bind": "/workspace", "mode": "rw"},
    }
    if kind is PreviewKind.LIVE and config.runtime.value in _NODE_RUNTIMES:
        # Build output belongs to the run, not to the sandbox worktree, or a
        # completion report fails on artifacts the project does not gitignore.
        for path in _BUILD_OUTPUT_PATHS:
            output = _data_volume(
                docker_client,
                run_id,
                f"build-{_slug(path)}",
                labels,
                False,
            )
            volumes[output.name] = {"bind": f"/workspace/{path}", "mode": "rw"}
            data_volumes.append(output)
    dependency_reused = False
    if config.runtime.value in _NODE_RUNTIMES:
        lockfile_digest = _lockfile_digest(
            _volume_runtime_files(docker_client, workspace_volume, settings)
        )
        dependency = _dependency_volume(
            docker_client,
            labels[LABEL_SANDBOX_ID],
            lockfile_digest,
            labels,
        )
        dependency_reused = _dependency_volume_ready(
            docker_client,
            settings,
            dependency.name,
        )
        volumes[dependency.name] = {"bind": "/workspace/node_modules", "mode": "rw"}
        data_volumes.append(dependency)
    elif config.runtime.value == "fastapi":
        dependency = _data_volume(docker_client, run_id, "python-venv", labels, False)
        volumes[dependency.name] = {"bind": "/opt/venv", "mode": "rw"}
        data_volumes.append(dependency)

    application_environment = _native_runtime_environment(config)
    application_environment.update(_secret_environment(config, secrets or {}))
    managed_database: SandboxDatabaseRuntime | None = None
    if controller_store is not None:
        try:
            managed_database = sandbox_database_runtime(
                docker_client,
                controller_store,
                labels[LABEL_SANDBOX_ID],
            )
        except SandboxDatabaseError as error:
            raise PreviewOperationError(error.status_code, error.detail) from error
    if managed_database is not None:
        application_environment.update(managed_database.environment)
        volumes.update(managed_database.volumes)

    if config.install_command:
        if config.runtime.value in _NODE_RUNTIMES:
            npm_cache = _data_volume(
                docker_client,
                run_id,
                "npm-cache",
                {**labels, LABEL_SERVICE: "npm-cache"},
                True,
            )
            volumes[npm_cache.name] = {"bind": "/root/.npm", "mode": "rw"}
            data_volumes.append(npm_cache)
        if dependency_reused:
            report(
                "dependencies",
                "Dependency volume already installed for this lockfile; skipping install",
                duration_ms=0,
            )
        else:
            with _timed_step(
                report,
                "dependencies",
                "Running the approved dependency installation command",
            ) as finish:
                install = config.install_command
                if config.runtime.value == "fastapi":
                    install = (
                        f"python -m venv /opt/venv\n. /opt/venv/bin/activate\n{install}"
                    )
                _run_prepare(
                    docker_client,
                    settings,
                    image=config.image,
                    command=f"set -eu\n{install}",
                    volumes=volumes,
                    mounts=mounts,
                    labels=labels,
                    environment=application_environment,
                    size_path=(
                        "/workspace/node_modules"
                        if config.runtime.value in _NODE_RUNTIMES
                        else "/opt/venv" if config.runtime.value == "fastapi" else None
                    ),
                    completion_marker=(
                        f"/workspace/node_modules/{_DEPENDENCY_READY_MARKER}"
                        if config.runtime.value in _NODE_RUNTIMES
                        else None
                    ),
                )
                finish("Dependency installation completed")
            if expected_protected_hashes is not None:
                report(
                    "protected-files",
                    "Checking protected runtime files after installation",
                )
                prepared_files = _volume_runtime_files(
                    docker_client,
                    project_volume,
                    settings,
                )
                if hashes(prepared_files) != expected_protected_hashes:
                    raise PreviewOperationError(
                        409,
                        "Dependency installation changed protected runtime files; "
                        "inspect again",
                    )

    if config.runtime.value in _NODE_RUNTIMES:
        # Vite's dependency optimizer creates node_modules/.vite on first run,
        # so a read-only mount fails with ENOENT and leaves the preview serving
        # unoptimized dependencies. The coding agent still mounts this volume
        # read-only; the install authority boundary that matters is the agent's.
        volumes[dependency.name] = {"bind": "/workspace/node_modules", "mode": "rw"}

    report("network", f"Creating {config.network_access.value} preview network")
    network = _network(docker_client, run_id, labels, config.network_access)
    containers: list[Container] = []
    if managed_database is not None:
        report(
            "database",
            f"Using sandbox database {managed_database.db_name}",
        )
    elif database is not None and database.sharing is not PreviewSharing.ISOLATED:
        if controller_store is None or not project_key:
            raise PreviewOperationError(
                422,
                "A shared database needs the controller store and project identity",
            )
        credentials, schema_name = _attach_shared_database(
            docker_client,
            controller_store,
            settings,
            sandbox_id=labels[LABEL_SANDBOX_ID],
            project_key=project_key,
            source_path=source_path,
            database=database,
            run_network=network,
            report=report,
        )
        application_environment.update(
            _native_service_environment(
                config,
                database,
                credentials,
                database_name=schema_name,
            )
        )
        if config.initialize.commands:
            report("initialize", "Running approved migration and seed commands")
            containers.append(
                _run_initialization(
                    docker_client,
                    settings,
                    image=config.image,
                    commands=config.initialize.commands,
                    runtime=config.runtime.value,
                    environment=application_environment,
                    volumes=volumes,
                    mounts=mounts,
                    tmpfs=tmpfs,
                    labels=labels,
                    network=network,
                    run_id=run_id,
                )
            )
            report("initialize", "Database initialization completed")
    elif database is not None:
        report("database-image", f"Checking database image {database.image}")
        _ensure_preview_image(docker_client, database.image)
        persistent = database.persistence is PreviewPersistence.PERSISTENT
        database_labels = {**labels, LABEL_SERVICE: "database"}
        database_volume = _data_volume(
            docker_client,
            run_id,
            "database",
            database_labels,
            persistent,
        )
        data_volumes.append(database_volume)
        credentials_volume = _data_volume(
            docker_client,
            run_id,
            "database-credentials",
            {**labels, LABEL_SERVICE: "database-credentials"},
            persistent,
        )
        data_volumes.append(credentials_volume)
        report("database", "Creating MySQL database container")
        provision = _database_engine.provision(
            DatabaseProvisionRequest(
                docker_client=docker_client,
                image=database.image,
                database=database.database,
                container_name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-database",
                labels=database_labels,
                data_volume=database_volume.name,
                credentials_volume=credentials_volume,
                network_name=network.name,
                memory_limit=settings.preview_memory,
                nano_cpus=1_000_000_000,
                pids_limit=256,
                error=PreviewOperationError,
            )
        )
        if provision is None:
            raise RuntimeError("MySQL container provisioning returned no container")
        credentials = provision.credentials
        application_environment.update(
            _native_service_environment(
                config,
                database,
                credentials,
            )
        )
        database_container = provision.container
        network.disconnect(database_container)
        network.connect(database_container, aliases=["database"])
        database_container.start()
        containers.append(database_container)
        report("database-health", "Waiting for MySQL health check")
        _wait_for_mysql_health(
            database_container,
            timeout_seconds=settings.prepare_timeout_seconds,
        )
        report("database-health", "MySQL is healthy")

        if config.initialize.commands:
            report("initialize", "Running approved migration and seed commands")
            initializer = _run_initialization(
                docker_client,
                settings,
                image=config.image,
                commands=config.initialize.commands,
                runtime=config.runtime.value,
                environment=application_environment,
                volumes=volumes,
                mounts=mounts,
                tmpfs=tmpfs,
                labels=labels,
                network=network,
                run_id=run_id,
            )
            containers.append(initializer)
            report("initialize", "Database initialization completed")

    start = config.start_command
    if config.runtime.value == "fastapi":
        start = f". /opt/venv/bin/activate\nexec {start}"
    else:
        start = f"exec {start}"
    with _timed_step(report, "container", "Creating application container") as finish:
        container = create_hardened(docker_client, HardenedContainerSpec(
            image=config.image,
            command=["sh", "-lc", f"set -eu\n{start}"],
            name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-app",
            working_dir="/workspace",
            environment=application_environment,
            labels={**labels, LABEL_SERVICE: "app"},
            volumes=volumes,
            mounts=mounts or None,
            tmpfs_size="256m",
            extra_tmpfs={key: value for key, value in tmpfs.items() if key != "/tmp"},
            network=network.name,
            egress=_preview_egress(config.network_access),
            ports=_direct_ports(config, host_port),
            restart_policy={"Name": "no"},
            mem_limit=settings.preview_memory,
            nano_cpus=1_000_000_000,
            pids_limit=256,
        ))
        network.disconnect(container)
        network.connect(container, aliases=["app"])
        if managed_database is not None and managed_database.engine != "sqlite":
            _connect_sandbox_database_endpoint(
                docker_client,
                managed_database,
                container,
            )
        container.start()
        _wait_for_container_health(
            container,
            timeout_seconds=settings.prepare_timeout_seconds,
        )
        finish("Application container started")
    containers.append(container)
    networks = [network]
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        report("gateway", "Creating the loopback preview gateway")
        gateway, gateway_network, gateway_volume = _gateway_proxy(
            docker_client,
            settings.inspection_image,
            network,
            "app",
            config.container_port,
            host_port,
            labels,
            run_id,
        )
        containers.append(gateway)
        networks.append(gateway_network)
        data_volumes.append(gateway_volume)
        report("gateway", "Loopback preview gateway started")
    return {
        "containers": containers,
        "networks": networks,
        "volumes": data_volumes,
        "images": [],
        "borrowed_networks": (
            [docker_client.networks.get(managed_database.network_name)]
            if managed_database is not None and managed_database.engine != "sqlite"
            else []
        ),
    }


def _export_commit(
    docker_client: DockerClient,
    git_image: str,
    project_volume: str,
    workspace_volume: str,
    commit_sha: str,
) -> None:
    """Replaces the run workspace with the tree of one sandbox commit.

    `git archive` is reproducible, leaves `.git` behind, and skips everything
    the repository ignores. The workspace is emptied first so a re-export after
    a restart cannot leave a file the commit no longer contains. Env files are
    deleted at every depth: a commit may carry one, and no preview reads them.
    """
    if not _COMMIT_PATTERN.match(commit_sha):
        raise PreviewOperationError(422, "Task commit is not a commit hash")
    name_clauses = " -o ".join(
        f"-name {shlex.quote(name)}" for name in _MASKED_ENVIRONMENT_NAMES
    )
    script = (
        "set -eu\n"
        "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf {} +\n"
        f"git -C /source archive --format=tar {commit_sha} | tar -C /workspace -xf -\n"
        f"find /workspace -type f \\( {name_clauses} \\) -delete\n"
    )
    run_git(
        docker_client,
        image=git_image,
        script=script,
        volumes={
            project_volume: {"bind": "/source", "mode": "ro"},
            workspace_volume: {"bind": "/workspace", "mode": "rw"},
        },
    )


def _environment_file_paths(
    docker_client: DockerClient,
    settings: PreviewSettings,
    volume_name: str,
) -> list[str]:
    """Lists every env file in the sandbox, relative to the volume root."""
    name_clauses = " -o ".join(
        f"-name {shlex.quote(name)}" for name in _MASKED_ENVIRONMENT_NAMES
    )
    command = (
        "set -eu\n"
        "cd /workspace\n"
        f"find . -type f \\( {name_clauses} \\) "
        "-not -path './.git/*' -not -path './node_modules/*' -print0\n"
    )
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
    )
    paths = []
    for entry in output.encode("utf-8", errors="replace").split(b"\0"):
        # -print0 keeps a newline in a filename from forging a second path.
        text = entry.decode("utf-8", errors="replace").removeprefix("./")
        if not text or ".." in PurePosixPath(text).parts:
            continue
        paths.append(text)
    return sorted(set(paths))


def _environment_masks(
    docker_client: DockerClient,
    settings: PreviewSettings,
    volume_name: str,
) -> list[Mount]:
    """Builds the mounts that make a preview's env files unreadable.

    A mount, not a copy-time exclusion: the container sees the mask for as long
    as it runs, so a coding agent writing `.env` after the preview started
    changes the sandbox and not what the preview reads. The two root paths are
    masked whether or not they exist yet, which is what closes that hole;
    deeper paths can only be masked where a file already sits, because Docker
    materialises a missing bind target inside the sandbox volume itself.
    """
    mask_source = _ensure_mask_source()
    paths = list(_MASKED_ENVIRONMENT_NAMES)
    for path in _environment_file_paths(docker_client, settings, volume_name):
        if path not in paths:
            paths.append(path)
    if len(paths) > _MAXIMUM_ENVIRONMENT_MASKS:
        raise PreviewOperationError(
            422,
            f"Sandbox holds more than {_MAXIMUM_ENVIRONMENT_MASKS} environment "
            "files; a preview cannot mask them all",
        )
    return [
        Mount(
            target=f"/workspace/{path}",
            source=mask_source,
            type="bind",
            read_only=True,
        )
        for path in paths
    ]


def _ensure_mask_source() -> str:
    """Returns a stable, empty regular file that Docker can bind over env files."""
    path = get_controller_settings().data_directory / _MASK_SOURCE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError("mask source is not a regular file")
        if not path.exists():
            path.touch(mode=0o600)
        elif path.stat().st_size:
            path.write_bytes(b"")
        path.chmod(0o600)
    except OSError as error:
        raise PreviewOperationError(
            503,
            "Preview environment masking is unavailable",
        ) from error
    return str(path)


def _exclude_preview_masks(
    docker_client: DockerClient,
    image: str,
    project_volume: str,
) -> None:
    """Keeps the root env masks out of `git status` in the sandbox.

    Docker creates an absent bind target as an empty file, so masking a `.env`
    that does not exist yet writes one into the sandbox volume. Untracked, it
    would fail every task completion report on the dirty-tree rule. The entries
    go in `.git/info/exclude`, which is local to the sandbox and not history.
    """
    marker = "# orchestrator preview masks"
    lines = "\\n".join((marker, *_MASKED_ENVIRONMENT_NAMES))
    script = (
        "set -eu\n"
        'exclude=/project/.git/info/exclude\n'
        '[ -d /project/.git ] || exit 0\n'
        'mkdir -p /project/.git/info\n'
        '[ -f "$exclude" ] || : > "$exclude"\n'
        f'if grep -qxF {shlex.quote(marker)} "$exclude"; then exit 0; fi\n'
        f'printf "{lines}\\n" >> "$exclude"\n'
    )
    _run_preview_command(
        docker_client,
        image=image,
        command=["sh", "-c", script],
        volumes={project_volume: {"bind": "/project", "mode": "rw"}},
        tmpfs_size="32m",
    )


def _native_service_environment(
    config: PreviewConfiguration,
    database: PreviewDependencyService,
    credentials: dict[str, str],
    *,
    database_name: str = "",
) -> dict[str, str]:
    return _database_engine.connection_url(
        DatabaseConnectionRequest(
            config=config,
            database=database,
            credentials=credentials,
            database_name=database_name,
            error=PreviewOperationError,
        )
    )


def _compose_service_environment(
    declared: Any,
    application_environment: dict[str, str],
    *,
    selected: bool,
) -> dict[str, str]:
    """Merges stored secrets into one Compose service.

    Only the selected service receives them. A sidecar keeps exactly the
    environment its Compose file declares.
    """
    environment = _compose_environment(declared)
    if selected:
        environment.update(application_environment)
    return environment


def _secret_environment(config: PreviewConfiguration, secrets: dict[str, str]) -> dict[str, str]:
    """Resolves from_secret entries against stored project secrets.

    Fails before any container starts, so a missing secret never surfaces as a
    runtime crash inside the preview.
    """
    environment: dict[str, str] = {}
    for variable, source in config.environment.items():
        if not source.from_secret:
            continue
        if source.from_secret not in secrets:
            raise PreviewOperationError(
                422,
                f"Preview secret {source.from_secret!r} is not configured",
            )
        environment[variable] = secrets[source.from_secret]
    return environment


def _native_runtime_environment(config: PreviewConfiguration) -> dict[str, str]:
    if config.runtime is PreviewRuntime.ASTRO:
        return {"ASTRO_TELEMETRY_DISABLED": "1"}
    return {}




def _run_initialization(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    image: str,
    commands: list[str],
    runtime: str,
    environment: dict[str, str],
    volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    network: Any,
    run_id: str,
    mounts: list[Mount] | None = None,
    tmpfs: dict[str, str] | None = None,
) -> Container:
    return _database_engine.run_migrations(
        DatabaseMigrationRequest(
            docker_client=docker_client,
            settings=settings,
            image=image,
            commands=commands,
            runtime=runtime,
            environment=environment,
            volumes=volumes,
            labels=labels,
            network=network,
            run_id=run_id,
            error=PreviewOperationError,
            mounts=mounts,
            tmpfs=tmpfs,
        )
    )


def _run_prepare(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    image: str,
    command: str,
    volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    size_path: str | None,
    completion_marker: str | None = None,
    environment: dict[str, str] | None = None,
    mounts: list[Mount] | None = None,
) -> None:
    container: Container | None = None
    try:
        checked_command = command
        if size_path:
            maximum_kib = settings.maximum_dependency_bytes // 1024
            checked_command += (
                f"\nused_kib=$(du -sk {shlex.quote(size_path)} | awk '{{print $1}}')"
                f"\nif [ \"$used_kib\" -gt {maximum_kib} ]; then"
                " echo 'Installed dependencies exceed the configured size limit' >&2;"
                " exit 73; fi"
            )
        if completion_marker:
            checked_command += f"\ntouch {shlex.quote(completion_marker)}"
        container = create_hardened(docker_client, HardenedContainerSpec(
            image=image,
            command=["sh", "-lc", checked_command],
            working_dir="/workspace",
            network="bridge",
            egress=Egress.PROVIDER,
            environment=environment,
            labels={**labels, LABEL_SERVICE: "prepare"},
            volumes=volumes,
            mounts=mounts or None,
            rootfs=Rootfs.WRITABLE,
            # No /tmp tmpfs. A package manager unpacks into /tmp, and this
            # container had a disk-backed /tmp before the boundary owned it.
            tmpfs_size=None,
            mem_limit=settings.preview_memory,
            pids_limit=256,
        ))
        container.start()
        try:
            result = container.wait(timeout=settings.prepare_timeout_seconds)
        except ReadTimeout as error:
            container.stop(timeout=2)
            raise PreviewOperationError(
                408,
                f"Dependency installation exceeded {settings.prepare_timeout_seconds} seconds",
            ) from error
        exit_code = int(result.get("StatusCode", 1))
        if exit_code != 0:
            logs = container.logs(stdout=True, stderr=True, tail=100)
            if isinstance(logs, bytes):
                detail = logs.decode("utf-8", errors="replace")[-8_192:]
            else:
                detail = str(logs)[-8_192:]
            raise PreviewOperationError(
                422,
                f"Dependency installation failed with code {exit_code}: {detail}",
            )
    finally:
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except DockerException:
                pass


def _start_dockerfile(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
    host_port: int,
    progress: ProgressReporter | None = None,
    secrets: dict[str, str] | None = None,
    controller_store: ControllerStore | None = None,
) -> dict[str, Any]:
    report = progress or _ignore_progress
    application_environment = _secret_environment(config, secrets or {})
    managed_database = _managed_preview_database(
        docker_client,
        controller_store,
        labels[LABEL_SANDBOX_ID],
    )
    if managed_database is not None:
        application_environment.update(managed_database.environment)
    report("build-context", "Exporting the current sandbox as a Docker build context")
    context = _volume_context_tar(
        docker_client,
        project_volume,
        ".",
        settings.inspection_image,
    )
    tag = f"orchestrator-preview:{run_id}"
    dockerfile = _safe_relative_path(config.dockerfile, field="dockerfile")
    report("build", f"Building image from {dockerfile}")
    try:
        built_image, _ = docker_client.images.build(
            fileobj=io.BytesIO(context),
            custom_context=True,
            dockerfile=dockerfile,
            tag=tag,
            rm=True,
            forcerm=True,
            labels=labels,
            timeout=settings.build_timeout_seconds,
        )
        _validate_built_image(built_image, settings)
    except (BuildError, APIError) as error:
        raise PreviewOperationError(422, f"Dockerfile build failed: {error}") from error
    report("build", "Docker image build completed")
    report("network", f"Creating {config.network_access.value} preview network")
    network = _network(docker_client, run_id, labels, config.network_access)
    report("container", "Creating application container")
    container = create_hardened(docker_client, HardenedContainerSpec(
        image=tag,
        name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-app",
        rootfs=Rootfs.WRITABLE,
        labels={**labels, LABEL_SERVICE: "app"},
        environment=application_environment,
        volumes={
            project_volume: {"bind": "/sandbox", "mode": "ro"},
            **(managed_database.volumes if managed_database is not None else {}),
        },
        tmpfs_size="256m",
        network=network.name,
        egress=_preview_egress(config.network_access),
        ports=_direct_ports(config, host_port),
        restart_policy={"Name": "no"},
        mem_limit=settings.preview_memory,
        nano_cpus=1_000_000_000,
        pids_limit=256,
    ))
    network.disconnect(container)
    network.connect(container, aliases=["app"])
    if managed_database is not None and managed_database.engine != "sqlite":
        _connect_sandbox_database_endpoint(docker_client, managed_database, container)
    container.start()
    report("container", "Application container started")
    containers = [container]
    networks = [network]
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        report("gateway", "Creating the loopback preview gateway")
        gateway, gateway_network, gateway_volume = _gateway_proxy(
            docker_client,
            settings.inspection_image,
            network,
            "app",
            config.container_port,
            host_port,
            labels,
            run_id,
        )
        containers.append(gateway)
        networks.append(gateway_network)
        data_volumes = [gateway_volume]
        report("gateway", "Loopback preview gateway started")
    else:
        data_volumes = []
    return {
        "containers": containers,
        "networks": networks,
        "volumes": data_volumes,
        "images": [built_image],
        "borrowed_networks": (
            [docker_client.networks.get(managed_database.network_name)]
            if managed_database is not None and managed_database.engine != "sqlite"
            else []
        ),
    }


def _start_compose(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    files: dict[str, bytes],
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
    host_port: int,
    progress: ProgressReporter | None = None,
    secrets: dict[str, str] | None = None,
    controller_store: ControllerStore | None = None,
) -> dict[str, Any]:
    report = progress or _ignore_progress
    # Stored secrets reach the selected service only. Sidecars keep the
    # environment their Compose file declares and nothing more.
    application_environment = _secret_environment(config, secrets or {})
    managed_database = _managed_preview_database(
        docker_client,
        controller_store,
        labels[LABEL_SANDBOX_ID],
    )
    if managed_database is not None:
        application_environment.update(managed_database.environment)
    compose_path = _safe_relative_path(config.compose_file, field="compose_file")
    report("compose", f"Reading Compose file {compose_path}")
    content = files.get(compose_path)
    if content is None:
        raise PreviewOperationError(422, "Approved Compose file is missing")
    try:
        document = yaml.safe_load(content.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise PreviewOperationError(422, "Compose file is invalid") from error
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        raise PreviewOperationError(422, "Compose file has no services")
    if config.selected_service not in services:
        raise PreviewOperationError(422, "Selected preview service is not in Compose")

    report("network", f"Creating {config.network_access.value} preview network")
    network = _network(docker_client, run_id, labels, config.network_access)
    data_volumes: list[Any] = []
    named_volumes: dict[str, Any] = {}
    containers: list[Container] = []
    try:
        for service_name, raw_service in services.items():
            if not isinstance(raw_service, dict):
                raise PreviewOperationError(422, f"Compose service '{service_name}' is invalid")
            _validate_compose_service(str(service_name), raw_service)
            action = "Building" if raw_service.get("build") is not None else "Checking"
            report("compose-image", f"{action} image for service {service_name}")
            image = _compose_image(
                docker_client,
                settings,
                project_volume,
                str(service_name),
                raw_service,
                labels,
                run_id,
            )
            mounts = _compose_volumes(
                docker_client,
                project_volume,
                raw_service.get("volumes") or [],
                named_volumes,
                data_volumes,
                config,
                labels,
                run_id,
            )
            mounts.setdefault(
                project_volume,
                {"bind": "/sandbox", "mode": "ro"},
            )
            if managed_database is not None and service_name == config.selected_service:
                mounts.update(managed_database.volumes)
            service_labels = {**labels, LABEL_SERVICE: str(service_name)}
            ports = (
                {f"{config.container_port}/tcp": ("127.0.0.1", host_port)}
                if service_name == config.selected_service
                and config.network_access is PreviewNetworkAccess.INTERNET
                else None
            )
            report("compose-container", f"Creating service container {service_name}")
            container = create_hardened(docker_client, HardenedContainerSpec(
                image=image,
                command=_command(raw_service.get("command")),
                entrypoint=_command(raw_service.get("entrypoint")),
                name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-{_slug(str(service_name))}",
                rootfs=(
                    Rootfs.READ_ONLY
                    if bool(raw_service.get("read_only", False))
                    else Rootfs.WRITABLE
                ),
                working_dir=raw_service.get("working_dir"),
                user=raw_service.get("user"),
                environment=_compose_service_environment(
                    raw_service.get("environment"),
                    application_environment,
                    selected=service_name == config.selected_service,
                ),
                labels=service_labels,
                volumes=mounts,
                tmpfs_size="256m",
                network=network.name,
                egress=_preview_egress(config.network_access),
                ports=ports,
                restart_policy={"Name": "no"},
                mem_limit=settings.preview_memory,
                nano_cpus=1_000_000_000,
                pids_limit=256,
            ))
            network.disconnect(container)
            network.connect(container, aliases=[str(service_name)])
            if (
                managed_database is not None
                and managed_database.engine != "sqlite"
                and service_name == config.selected_service
            ):
                _connect_sandbox_database_endpoint(
                    docker_client,
                    managed_database,
                    container,
                )
            containers.append(container)

        by_service = {
            ((container.attrs.get("Config") or {}).get("Labels") or {}).get(
                LABEL_SERVICE, ""
            ): container
            for container in containers
        }
        for service_name in _service_order(services):
            report("compose-start", f"Starting service {service_name}")
            by_service[service_name].start()
    except Exception:
        _remove_resources(
            {
                "containers": containers,
                "networks": [network],
                "volumes": data_volumes,
                "images": [],
            },
            remove_data_volumes=True,
        )
        raise
    networks = [network]
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        report("gateway", "Creating the loopback preview gateway")
        gateway, gateway_network, gateway_volume = _gateway_proxy(
            docker_client,
            settings.inspection_image,
            network,
            config.selected_service,
            config.container_port,
            host_port,
            labels,
            run_id,
        )
        containers.append(gateway)
        networks.append(gateway_network)
        data_volumes.append(gateway_volume)
        report("gateway", "Loopback preview gateway started")
    return {
        "containers": containers,
        "networks": networks,
        "volumes": data_volumes,
        "images": _preview_images(docker_client, run_id),
        "borrowed_networks": (
            [docker_client.networks.get(managed_database.network_name)]
            if managed_database is not None and managed_database.engine != "sqlite"
            else []
        ),
    }




def _validate_compose_service(service_name: str, service: dict[str, Any]) -> None:
    forbidden = {
        "privileged",
        "network_mode",
        "pid",
        "ipc",
        "devices",
        "cap_add",
        # Compose cannot request Docker's no-new-privileges control.  Build
        # the key here so the boundary guard can reserve its direct spelling.
        "_".join(("security", "opt")),
        "env_file",
        "secrets",
        "configs",
    }
    present = sorted(key for key in forbidden if service.get(key))
    if present:
        raise PreviewOperationError(
            422,
            f"Compose service '{service_name}' uses blocked fields: {', '.join(present)}",
        )


def _compose_image(
    docker_client: DockerClient,
    settings: PreviewSettings,
    project_volume: str,
    service_name: str,
    service: dict[str, Any],
    labels: dict[str, str],
    run_id: str,
) -> str:
    build = service.get("build")
    if build is not None:
        if isinstance(build, str):
            context = build
            dockerfile = "Dockerfile"
        elif isinstance(build, dict):
            context = str(build.get("context", "."))
            dockerfile = str(build.get("dockerfile", "Dockerfile"))
            if build.get("ssh") or build.get("secrets") or build.get("privileged"):
                raise PreviewOperationError(
                    422,
                    f"Compose service '{service_name}' requests blocked build privileges",
                )
        else:
            raise PreviewOperationError(422, f"Compose service '{service_name}' has invalid build")
        context_path = _safe_relative_path(context, field="build context", allow_dot=True)
        dockerfile_path = _safe_relative_path(dockerfile, field="dockerfile")
        archive = _volume_context_tar(
            docker_client,
            project_volume,
            context_path,
            settings.inspection_image,
        )
        tag = f"orchestrator-preview:{run_id}-{_slug(service_name)}"
        try:
            built_image, _ = docker_client.images.build(
                fileobj=io.BytesIO(archive),
                custom_context=True,
                dockerfile=dockerfile_path,
                tag=tag,
                rm=True,
                forcerm=True,
                labels=labels,
                timeout=settings.build_timeout_seconds,
            )
            _validate_built_image(built_image, settings)
        except (BuildError, APIError) as error:
            raise PreviewOperationError(
                422,
                f"Compose service '{service_name}' build failed: {error}",
            ) from error
        return tag
    image = service.get("image")
    if not isinstance(image, str) or not image:
        raise PreviewOperationError(
            422,
            f"Compose service '{service_name}' requires image or build",
        )
    if "${" in image:
        raise PreviewOperationError(422, "Compose environment interpolation is disabled")
    _ensure_preview_image(docker_client, image)
    return image


def _compose_volumes(
    docker_client: DockerClient,
    project_volume: str,
    declarations: Any,
    named_volumes: dict[str, Any],
    data_volumes: list[Any],
    config: PreviewConfiguration,
    labels: dict[str, str],
    run_id: str,
) -> dict[str, dict[str, str]]:
    if not isinstance(declarations, list):
        raise PreviewOperationError(422, "Compose service volumes must be a list")
    mounts: dict[str, dict[str, str]] = {}
    for declaration in declarations:
        source: str
        target: str
        mode = "rw"
        mount_type = "volume"
        if isinstance(declaration, str):
            pieces = declaration.split(":")
            if len(pieces) == 1:
                source = ""
                target = pieces[0]
            elif len(pieces) in {2, 3}:
                source, target = pieces[:2]
                if len(pieces) == 3 and pieces[2] == "ro":
                    mode = "ro"
            else:
                raise PreviewOperationError(422, "Compose volume syntax is unsupported")
            if source.startswith(".") or source.startswith("/"):
                mount_type = "bind"
        elif isinstance(declaration, dict):
            mount_type = str(declaration.get("type", "volume"))
            source = str(declaration.get("source", ""))
            target = str(declaration.get("target", ""))
            if declaration.get("read_only"):
                mode = "ro"
        else:
            raise PreviewOperationError(422, "Compose volume declaration is invalid")
        if not target.startswith("/"):
            raise PreviewOperationError(422, "Compose volume target must be absolute")
        if mount_type == "bind":
            if source not in {".", "./"}:
                raise PreviewOperationError(
                    422,
                    "Compose host bind mounts are blocked; only the sandbox root may be mounted",
                )
            mounts[project_volume] = {"bind": target, "mode": mode}
            continue
        logical_name = source or f"anonymous-{len(data_volumes) + 1}"
        volume = named_volumes.get(logical_name)
        if volume is None:
            persistent = logical_name in config.persistent_volumes
            volume = _data_volume(
                docker_client,
                run_id,
                logical_name,
                labels,
                persistent,
            )
            named_volumes[logical_name] = volume
            data_volumes.append(volume)
        mounts[volume.name] = {"bind": target, "mode": mode}
    return mounts


def _compose_environment(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        environment = {str(key): "" if item is None else str(item) for key, item in value.items()}
    elif isinstance(value, list):
        environment = {}
        for entry in value:
            key, separator, item = str(entry).partition("=")
            if not separator:
                raise PreviewOperationError(
                    422,
                    "Compose environment variables must include explicit values",
                )
            environment[key] = item
    else:
        raise PreviewOperationError(422, "Compose environment is invalid")
    if any("${" in item for item in environment.values()):
        raise PreviewOperationError(422, "Compose environment interpolation is disabled")
    return environment


def _service_order(services: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise PreviewOperationError(422, "Compose dependency cycle is unsupported")
        visiting.add(name)
        service = services.get(name) or {}
        dependencies = service.get("depends_on") or []
        dependency_names = dependencies if isinstance(dependencies, list) else dependencies.keys()
        for dependency in dependency_names:
            dependency_name = str(dependency)
            if dependency_name not in services:
                raise PreviewOperationError(
                    422,
                    f"Compose service '{name}' depends on missing service '{dependency_name}'",
                )
            visit(dependency_name)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for service_name in services:
        visit(str(service_name))
    return ordered


def _command(value: Any) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return [str(item) for item in value]
    raise PreviewOperationError(422, "Compose command or entrypoint is invalid")


def _network(
    docker_client: DockerClient,
    run_id: str,
    labels: dict[str, str],
    access: PreviewNetworkAccess,
) -> Any:
    return docker_client.networks.create(
        f"orchestrator-preview-{run_id[:12]}",
        driver="bridge",
        internal=access is PreviewNetworkAccess.ISOLATED,
        labels=labels,
    )


def _preview_egress(access: PreviewNetworkAccess) -> Egress:
    """Keep internet previews on their existing external bridge."""
    return Egress.PROVIDER if access is PreviewNetworkAccess.INTERNET else Egress.DENIED


def _direct_ports(
    config: PreviewConfiguration,
    host_port: int,
) -> dict[str, tuple[str, int]] | None:
    if config.network_access is PreviewNetworkAccess.ISOLATED:
        return None
    return {f"{config.container_port}/tcp": ("127.0.0.1", host_port)}


def _gateway_proxy(
    docker_client: DockerClient,
    image: str,
    service_network: Any,
    target_service: str,
    target_port: int,
    host_port: int,
    labels: dict[str, str],
    run_id: str,
) -> tuple[Container, Any, Any]:
    _ensure_preview_image(docker_client, image)
    gateway_network = docker_client.networks.create(
        f"orchestrator-preview-{run_id[:12]}-gateway",
        driver="bridge",
        internal=False,
        labels=labels,
    )
    gateway_volume = _data_volume(
        docker_client,
        run_id,
        "gateway-script",
        labels,
        False,
    )
    script = f"#!/bin/sh\nexec nc {shlex.quote(target_service)} {target_port}\n"
    _run_preview_command(
        docker_client,
        image=image,
        command=[
            "sh",
            "-c",
            "set -eu; printf '%s' \"$FORWARD_SCRIPT\" > /proxy/forward; chmod 700 /proxy/forward",
        ],
        environment={"FORWARD_SCRIPT": script},
        volumes={gateway_volume.name: {"bind": "/proxy", "mode": "rw"}},
    )
    gateway: Container | None = None
    try:
        gateway = create_hardened(docker_client, HardenedContainerSpec(
            image=image,
            command=["nc", "-lk", "-p", "8080", "-e", "/proxy/forward"],
            name=f"{PREVIEW_CONTAINER_PREFIX}{run_id[:12]}-gateway",
            labels={**labels, LABEL_SERVICE: "gateway"},
            volumes={gateway_volume.name: {"bind": "/proxy", "mode": "ro"}},
            network=gateway_network.name,
            ports={"8080/tcp": ("127.0.0.1", host_port)},
            restart_policy={"Name": "no"},
            mem_limit="128m",
            nano_cpus=250_000_000,
            pids_limit=32,
        ))
        service_network.connect(gateway, aliases=["preview-gateway"])
        gateway.start()
    except Exception:
        if gateway is not None:
            try:
                gateway.remove(force=True, v=True)
            except DockerException:
                pass
        try:
            gateway_network.remove()
        except DockerException:
            pass
        try:
            gateway_volume.remove(force=True)
        except DockerException:
            pass
        raise
    return gateway, gateway_network, gateway_volume




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
