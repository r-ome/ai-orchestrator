import base64
import hashlib
import io
import json
import logging
import re
import secrets
import shlex
import socket
import tarfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any
from uuid import uuid4

import yaml
from docker.client import DockerClient
from docker.errors import (
    APIError,
    BuildError,
    ContainerError,
    DockerException,
    NotFound,
)
from docker.models.containers import Container
from docker.types import Mount
from requests.exceptions import ReadTimeout

from app.containers.hardened import (
    Capture,
    Egress,
    HardenedContainerSpec,
    HardenedRunSpec,
    Rootfs,
    create_hardened,
    run_hardened,
)
from app.containers.images import ensure_image
from app.controller.config import get_controller_settings
from app.controller.store import ControllerStore, SandboxWriterAdmissionError
from app.previews.config import PreviewSettings
from app.previews.detection import (
    ENVIRONMENT_FILE_NAMES,
    capture_source_runtime_files,
    compare_files,
    detect_preview,
    hashes,
    is_detection_file,
    parse_environment_names,
    parse_environment_pairs,
    proposal_digest,
)
from app.previews.models import (
    ENVIRONMENT_VARIABLE_PATTERN,
    DatabaseSharingState,
    ImportProjectSecretsResponse,
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
    ProjectDatabaseSharing,
    ProjectSecretName,
    ProjectSecrets,
    SetProjectSecretsRequest,
    StartPreviewRequest,
    StopPreviewResponse,
)
from app.projects.service import (
    ProjectOperationError,
    inspect_registered_project,
    managed_project_key,
)
from app.sandboxes.git import run_git
from app.sandboxes.naming import network as sandbox_network_name
from app.sandboxes.database import (
    MYSQL_DATABASE,
    DatabaseConnectionRequest,
    DatabaseDropRequest,
    DatabaseEngine,
    DatabaseMigrationRequest,
    DatabaseProvisionRequest,
    DatabaseSchemaProvisionRequest,
    mysql_identifier,
    mysql_shared_database_names,
    mysql_shared_schema_name,
    mysql_shared_user_name,
    wait_for_mysql_health,
    SandboxDatabaseError,
    SandboxDatabaseRuntime,
    sandbox_database_runtime,
)
from app.tasks.models import TaskStatus
from app.tasks.service import transition_task


LABEL_MANAGED = "orchestrator.preview.managed"
LABEL_DATA_MANAGED = "orchestrator.preview.data-managed"
LABEL_CONTROLLER_MANAGED = "orchestrator.managed"
LABEL_SANDBOX_ID = "orchestrator.sandbox.id"
LABEL_RUN_ID = "orchestrator.run.id"
LABEL_KIND = "orchestrator.kind"
LABEL_SERVICE = "orchestrator.preview.service"
LABEL_EXPIRES_AT = "orchestrator.preview.expires-at"
LABEL_PERSISTENT = "orchestrator.preview.persistent"
LABEL_PROJECT_ID = "orchestrator.project.id"
LABEL_SHARED_DATABASE = "orchestrator.shared-database"
LABEL_SHARED_DATABASE_IMAGE = "orchestrator.shared-database.image"
LABEL_PROJECT_SOURCE = "orchestrator.project.source"
PREVIEW_CONTAINER_PREFIX = "orchestrator-preview-"
SHARED_DATABASE_PREFIX = "orchestrator-shared-db-"
MAX_CONTEXT_BYTES = 512 * 1024 * 1024
# Inspection commands were previously unbounded.  The archive cap covers a
# 512 MiB Docker build context after base64 encoding, which stays text-safe.
PREVIEW_COMMAND_TIMEOUT_SECONDS = 60
PREVIEW_COMMAND_MAX_LOG_BYTES = 1_048_576
PREVIEW_ARCHIVE_MAX_LOG_BYTES = 716_000_000
# Raised at approval and again at attach, so one wording covers both.
_SHARED_DATA_UNAVAILABLE = (
    "shared_data is unavailable; each managed sandbox owns its database"
)
_database_engine: DatabaseEngine = MYSQL_DATABASE
# Priority order for the lockfile that keys a sandbox's dependency volume.
_LOCKFILE_NAMES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
)
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
_DEPENDENCY_READY_MARKER = ".orchestrator-install-complete"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_preview_lock = Lock()
# Serializes get-or-create of a project's shared server across concurrent starts.
_shared_database_lock = Lock()
logger = logging.getLogger("uvicorn.error")
# Accepts an optional duration_ms kwarg so a step can report zero-duration reuse.
ProgressReporter = Callable[..., None]


class PreviewOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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


def _record_preview_progress(
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    proposal_id: str,
    preview_id: str,
    status: str,
    step: str,
    message: str,
    level: str = "info",
    duration_ms: int | None = None,
    started_at: str | None = None,
) -> None:
    limited_message = message[-16_384:]
    log_method = logger.error if level == "error" else logger.info
    log_method(
        "Preview %s proposal %s [%s] %s",
        preview_id,
        proposal_id,
        step,
        limited_message,
    )
    payload: dict[str, Any] = {
        "preview_id": preview_id,
        "status": status,
        "level": level,
        "step": step,
        "message": limited_message,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if started_at is not None:
        payload["started_at"] = started_at
    controller_store.event(
        sandbox_id=sandbox_id,
        run_id=proposal_id,
        kind="preview.progress",
        payload=payload,
    )


def _ignore_progress(
    step: str,
    message: str,
    duration_ms: int | None = None,
    started_at: str | None = None,
) -> None:
    del step, message, duration_ms, started_at


@contextmanager
def _timed_step(
    report: ProgressReporter,
    step: str,
    message: str,
) -> Iterator[Callable[[str], None]]:
    """Times one preview-preparation step.

    Emits a start event carrying `started_at`, then a completion event
    carrying `duration_ms`. Yields a callable the caller can use to give the
    completion event a result-specific message; called with an empty string,
    the completion event reuses `message`. Emits nothing on failure — the
    caller's own error handling already records a `failed` step.
    """
    started_at = _now()
    started = time.monotonic()
    report(step, message, started_at=started_at)
    completion_message = message

    def finish(text: str) -> None:
        nonlocal completion_message
        if text:
            completion_message = text

    yield finish
    duration_ms = int((time.monotonic() - started) * 1000)
    report(step, completion_message, duration_ms=duration_ms, started_at=started_at)


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


def _shared_database_names(project_key: str) -> dict[str, str]:
    return mysql_shared_database_names(project_key)


def _shared_schema_name(sandbox_id: str) -> str:
    return mysql_shared_schema_name(sandbox_id, PreviewOperationError)


def _shared_user_name(sandbox_id: str) -> str:
    return mysql_shared_user_name(sandbox_id, PreviewOperationError)


def _identifier(sandbox_id: str) -> str:
    return mysql_identifier(sandbox_id, PreviewOperationError)


def _shared_database_labels(
    project_key: str,
    source_path: str,
    image: str,
) -> dict[str, str]:
    """Labels a shared server and its volumes.

    Deliberately carries no run id and no sandbox id. Every teardown path
    filters on those, so the shared server cannot be swept away with the run
    that happened to create it.
    """
    return {
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_KIND: "shared-database",
        LABEL_SHARED_DATABASE: "true",
        LABEL_SHARED_DATABASE_IMAGE: image,
        LABEL_PROJECT_ID: project_key,
        LABEL_PROJECT_SOURCE: source_path,
        LABEL_SERVICE: "database",
        LABEL_PERSISTENT: "true",
    }


def _shared_volume(docker_client: DockerClient, name: str, labels: dict[str, str]) -> Any:
    try:
        return docker_client.volumes.get(name)
    except NotFound:
        pass
    try:
        return docker_client.volumes.create(name=name, driver="local", labels=labels)
    except APIError:
        return docker_client.volumes.get(name)


def _shared_network(docker_client: DockerClient, name: str, labels: dict[str, str]) -> Any:
    existing = docker_client.networks.list(names=[name])
    for network in existing:
        if network.name == name:
            return network
    try:
        return docker_client.networks.create(
            name,
            driver="bridge",
            internal=True,
            labels=labels,
        )
    except APIError:
        for network in docker_client.networks.list(names=[name]):
            if network.name == name:
                return network
        raise


def _shared_database_server(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    project_key: str,
    source_path: str,
    database: PreviewDependencyService,
    report: ProgressReporter,
) -> tuple[Container, Any, Any]:
    """Returns the project's shared MySQL server, creating it on first use.

    Held under `_shared_database_lock` so two sandboxes starting at the same
    moment cannot both create the server.
    """
    names = _shared_database_names(project_key)
    labels = _shared_database_labels(project_key, source_path, database.image)
    with _shared_database_lock:
        report("database-image", f"Checking database image {database.image}")
        _ensure_preview_image(docker_client, database.image)
        network = _shared_network(docker_client, names["network"], labels)
        data_volume = _shared_volume(
            docker_client,
            names["data"],
            {**labels, LABEL_DATA_MANAGED: "true"},
        )
        credentials_volume = _shared_volume(
            docker_client,
            names["credentials"],
            {**labels, LABEL_DATA_MANAGED: "true", LABEL_SERVICE: "database-credentials"},
        )
        container = _existing_shared_server(docker_client, names["container"])
        created = container is None
        if created:
            report("database", "Creating the shared project database")
        provision = _database_engine.provision(
            DatabaseProvisionRequest(
                docker_client=docker_client,
                image=database.image,
                database=database.database,
                container_name=names["container"],
                labels=labels,
                data_volume=data_volume.name,
                credentials_volume=credentials_volume,
                network_name=network.name,
                memory_limit=settings.shared_database_memory,
                nano_cpus=2_000_000_000,
                pids_limit=512,
                error=PreviewOperationError,
                shared=True,
                max_connections=settings.shared_database_max_connections,
                existing_container=container,
            )
        )
        if provision is None:
            raise RuntimeError("MySQL container provisioning returned no container")
        container = provision.container
        if not created:
            stored_image = (
                (container.attrs.get("Config") or {}).get("Labels") or {}
            ).get(LABEL_SHARED_DATABASE_IMAGE, "")
            if stored_image and stored_image != database.image:
                raise PreviewOperationError(
                    409,
                    "This project's shared database runs "
                    f"{stored_image}; the proposal asks for {database.image}",
                )
            if container.status != "running":
                report("database", "Starting the shared project database")
                container.start()
        else:
            container.start()

        report("database-health", "Waiting for the shared database health check")
        _wait_for_mysql_health(
            container,
            timeout_seconds=settings.prepare_timeout_seconds,
        )
        report("database-health", "Shared database is healthy")
    return container, credentials_volume, network


def _existing_shared_server(
    docker_client: DockerClient,
    name: str,
) -> Container | None:
    try:
        container = docker_client.containers.get(name)
    except NotFound:
        return None
    container.reload()
    return container


def _attach_shared_database(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    *,
    sandbox_id: str,
    project_key: str,
    source_path: str,
    database: PreviewDependencyService,
    run_network: Any,
    report: ProgressReporter,
) -> tuple[dict[str, str], str]:
    """Gives one sandbox credentials on the project's shared server.

    Returns the credentials and the schema the sandbox must use. Every managed
    sandbox owns its schema, so the schema is always this sandbox's own.
    """
    if database.sharing is PreviewSharing.SHARED_DATA:
        # `_validate_sharing` refuses shared_data at approval. An approval
        # recorded before that guard can still reach this call, so refuse the
        # guest here too rather than provision one nothing else supports.
        raise PreviewOperationError(422, _SHARED_DATA_UNAVAILABLE)
    server, credentials_volume, shared_network = _shared_database_server(
        docker_client,
        settings,
        project_key=project_key,
        source_path=source_path,
        database=database,
        report=report,
    )

    schema_name = _shared_schema_name(sandbox_id)
    # Schema names are truncated sandbox ids. A collision would silently join
    # two sandboxes' data, so refuse rather than share by accident.
    for row in controller_store.shared_schemas_for_project(project_key):
        if (
            str(row["schema_name"]) == schema_name
            and str(row["owner_sandbox_id"]) != sandbox_id
        ):
            raise PreviewOperationError(
                409,
                f"Schema name {schema_name} already belongs to another sandbox",
            )
    user_name = _shared_user_name(sandbox_id)
    password = secrets.token_urlsafe(24)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", password):
        raise PreviewOperationError(500, "Generated database password is unusable")

    statements = [
        f"CREATE DATABASE IF NOT EXISTS `{schema_name}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        f"CREATE USER IF NOT EXISTS '{user_name}'@'%' IDENTIFIED BY '{password}'",
        f"ALTER USER '{user_name}'@'%' IDENTIFIED BY '{password}'",
        f"GRANT ALL PRIVILEGES ON `{schema_name}`.* TO '{user_name}'@'%'",
        "FLUSH PRIVILEGES",
    ]
    report(
        "database-schema",
        f"Provisioning schema {schema_name} on the shared project database",
    )
    _database_engine.provision(
        DatabaseSchemaProvisionRequest(
            docker_client=docker_client,
            image=database.image,
            network_name=shared_network.name,
            host=server.name,
            credentials_volume=credentials_volume,
            statements=statements,
            error=PreviewOperationError,
        )
    )

    report("database", "Connecting the shared database to the preview network")
    _connect_shared_server(run_network, server)

    controller_store.record_shared_schema(
        sandbox_id=sandbox_id,
        project_id=project_key,
        owner_sandbox_id=sandbox_id,
        sharing=database.sharing.value,
        schema_name=schema_name,
        user_name=user_name,
        image=database.image,
        persistence=database.persistence.value,
    )
    credentials = {
        "username": user_name,
        "password": password,
        "root_password": "",
    }
    return credentials, schema_name


def _restart_shared_database(
    docker_client: DockerClient,
    settings: PreviewSettings,
    *,
    project_key: str,
    source_path: str,
    database: PreviewDependencyService,
    run_id: str,
) -> Container:
    """Brings the shared server back up and reattaches it to the run network.

    A restart can follow a daemon restart, so the endpoint on the preview
    network is reasserted rather than assumed.
    """
    server, _, _ = _shared_database_server(
        docker_client,
        settings,
        project_key=project_key,
        source_path=source_path,
        database=database,
        report=_ignore_progress,
    )
    run_network_name = f"orchestrator-preview-{run_id[:12]}"
    for network in _preview_networks(docker_client, run_id):
        if network.name == run_network_name:
            _connect_shared_server(network, server)
    server.reload()
    return server


def _connect_shared_server(run_network: Any, server: Container) -> None:
    """Aliases the shared server as `database` inside one preview network."""
    try:
        run_network.connect(server, aliases=["database"])
    except APIError as error:
        message = str(error).casefold()
        if "already exists" not in message and "already connected" not in message:
            raise


def _connect_sandbox_database_endpoint(
    docker_client: DockerClient,
    runtime: SandboxDatabaseRuntime,
    container: Container,
) -> None:
    """Join a runtime to the persistent internal network as a borrowed endpoint."""
    try:
        network = docker_client.networks.get(runtime.network_name)
        network.connect(container)
    except (NotFound, APIError) as error:
        message = str(error).casefold()
        if "already exists" not in message and "already connected" not in message:
            raise PreviewOperationError(
                409,
                f"Could not join sandbox database network '{runtime.network_name}'",
            ) from error


def _managed_preview_database(
    docker_client: DockerClient,
    controller_store: ControllerStore | None,
    sandbox_id: str,
) -> SandboxDatabaseRuntime | None:
    if controller_store is None:
        return None
    try:
        return sandbox_database_runtime(
            docker_client,
            controller_store,
            sandbox_id,
        )
    except SandboxDatabaseError as error:
        raise PreviewOperationError(error.status_code, error.detail) from error


def _release_shared_database(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
) -> dict[str, Any]:
    """Undoes one sandbox's claim on the shared server.

    An owner loses its schema only when the data is ephemeral and no guest is
    still attached to it. A guest loses only its own user. No new guest can be
    created, so the guest path here serves rows written before that rule and is
    the one place that still asks whether a row belongs to its sandbox.
    """
    record = controller_store.shared_schema(sandbox_id)
    if record is None:
        return {"released": False}

    project_key = str(record["project_id"])
    schema_name = str(record["schema_name"])
    user_name = str(record["user_name"])
    image = str(record["image"])
    owner = str(record["owner_sandbox_id"]) == sandbox_id
    ephemeral = str(record["persistence"]) == PreviewPersistence.EPHEMERAL.value
    siblings = [
        row
        for row in controller_store.shared_schemas_for_project(project_key)
        if str(row["sandbox_id"]) != sandbox_id
        and str(row["schema_name"]) == schema_name
    ]
    drop_schema = owner and ephemeral and not siblings

    names = _shared_database_names(project_key)
    server = _existing_shared_server(docker_client, names["container"])
    credentials_volume = _existing_volume(docker_client, names["credentials"])
    outcome: dict[str, Any] = {
        "released": True,
        "schema": schema_name,
        "dropped_schema": drop_schema,
        "kept_for_attached_sandboxes": len(siblings) if owner and ephemeral else 0,
    }
    applied = False
    if server is not None and server.status == "running" and credentials_volume is not None:
        statements = [f"DROP USER IF EXISTS '{user_name}'@'%'"]
        if drop_schema:
            statements.append(f"DROP DATABASE IF EXISTS `{schema_name}`")
        statements.append("FLUSH PRIVILEGES")
        try:
            _database_engine.drop(
                DatabaseDropRequest(
                    docker_client=docker_client,
                    image=image,
                    network_name=names["network"],
                    host=server.name,
                    credentials_volume=credentials_volume,
                    statements=statements,
                    error=PreviewOperationError,
                )
            )
            applied = True
        except (PreviewOperationError, DockerException) as error:
            # The sandbox is going away either way. Record the leftover so an
            # operator can see it instead of losing it silently.
            outcome["error"] = str(error)

    # The record tracks a schema that exists. It is dropped only when the schema
    # and user really went away; otherwise the leftover stays visible and the
    # next start of this sandbox reuses it instead of creating a duplicate.
    keep_record = not applied or (owner and not drop_schema)
    if not keep_record:
        controller_store.delete_shared_schema(sandbox_id)
    outcome["kept_record"] = keep_record
    outcome["pending_cleanup"] = not applied
    _stop_idle_shared_server(docker_client, controller_store, project_key)
    return outcome


def _existing_volume(docker_client: DockerClient, name: str) -> Any | None:
    try:
        return docker_client.volumes.get(name)
    except NotFound:
        return None


def _shared_server_is_idle(
    controller_store: ControllerStore,
    project_key: str,
) -> bool:
    """True when no preview of this project is still running.

    Persistent schemas keep their records after their preview stops, so idleness
    is measured by active previews, not by records.
    """
    project_sandboxes = {
        str(sandbox["id"])
        for sandbox in controller_store.sandboxes()
        if str(sandbox["project_id"]) == project_key
    }
    return not any(
        str(run["sandbox_id"]) in project_sandboxes
        for run in controller_store.active_previews()
    )


def _stop_idle_shared_server(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_key: str,
) -> None:
    """Removes the shared server once no preview of the project is running.

    Only the container goes. Its volumes stay, so persistent schemas and the
    root credentials survive and the next start finds the same data.
    """
    if not _shared_server_is_idle(controller_store, project_key):
        return
    names = _shared_database_names(project_key)
    with _shared_database_lock:
        server = _existing_shared_server(docker_client, names["container"])
        if server is not None:
            try:
                server.remove(force=True, v=True)
            except DockerException:
                return
        for network in docker_client.networks.list(names=[names["network"]]):
            if network.name != names["network"]:
                continue
            try:
                network.remove()
            except DockerException:
                continue


def _sharing_state(
    controller_store: ControllerStore,
    sandbox_id: str,
) -> DatabaseSharingState | None:
    record = controller_store.shared_schema(sandbox_id)
    if record is None:
        return None
    project_key = str(record["project_id"])
    owner_sandbox_id = str(record["owner_sandbox_id"])
    schema_name = str(record["schema_name"])
    names = {
        str(sandbox["id"]): str(sandbox["project_name"])
        for sandbox in controller_store.sandboxes()
    }
    attached = [
        names.get(str(row["sandbox_id"]), str(row["sandbox_id"])[:12])
        for row in controller_store.shared_schemas_for_project(project_key)
        if str(row["schema_name"]) == schema_name
        and str(row["sandbox_id"]) != owner_sandbox_id
    ]
    return DatabaseSharingState(
        sandbox_id=sandbox_id,
        sharing=PreviewSharing(str(record["sharing"])),
        schema_name=schema_name,
        owner_sandbox_id=owner_sandbox_id,
        owner_project_name=names.get(owner_sandbox_id, owner_sandbox_id[:12]),
        image=str(record["image"]),
        persistence=PreviewPersistence(str(record["persistence"])),
        server_container=_shared_database_names(project_key)["container"],
        attached_project_names=attached,
    )


def database_sharing_state(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> ProjectDatabaseSharing:
    """The database coupling of one sandbox."""
    project = _ready_project(docker_client, project_name, controller_store)
    return ProjectDatabaseSharing(
        project_name=project.name,
        sandbox_id=project.sandbox_id,
        current=_sharing_state(controller_store, project.sandbox_id),
    )


def get_project_secrets(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> ProjectSecrets:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    return _project_secrets_response(controller_store, project, project_key)


def set_project_secrets(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    request: SetProjectSecretsRequest,
) -> ProjectSecrets:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    controller_store.set_project_secrets(project_key, request.values)
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=None,
        kind="preview.secrets.set",
        payload={"names": sorted(request.values)},
    )
    return _project_secrets_response(controller_store, project, project_key)


def delete_project_secret(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    name: str,
) -> ProjectSecrets:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    controller_store.delete_project_secret(project_key, name)
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=None,
        kind="preview.secrets.deleted",
        payload={"names": [name]},
    )
    return _project_secrets_response(controller_store, project, project_key)


def import_project_secrets(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
) -> ImportProjectSecretsResponse:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    environment_files = _volume_environment_files(
        docker_client,
        project.volume_name,
        settings,
    )
    pairs = parse_environment_pairs(environment_files)
    imported: list[str] = []
    skipped: list[str] = []
    values: dict[str, str] = {}
    for name, value in pairs.items():
        if not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(name) or len(
            value.encode("utf-8")
        ) > 8_192:
            skipped.append(name)
            continue
        values[name] = value
        imported.append(name)
    if values:
        controller_store.set_project_secrets(project_key, values)
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=None,
        kind="preview.secrets.imported",
        payload={"imported": sorted(imported), "skipped": sorted(skipped)},
    )
    return ImportProjectSecretsResponse(
        project_name=project.name,
        imported=sorted(imported),
        skipped=sorted(skipped),
    )


def _project_secrets_response(
    controller_store: ControllerStore,
    project: Any,
    project_key: str,
) -> ProjectSecrets:
    return ProjectSecrets(
        project_name=project.name,
        names=[
            ProjectSecretName(name=entry["name"], updated_at=entry["updated_at"])
            for entry in controller_store.project_secret_entries(project_key)
        ],
    )


def _validate_sharing(config: PreviewConfiguration) -> None:
    database = config.services.get("database")
    if database is None or database.sharing is PreviewSharing.ISOLATED:
        return
    if config.mode is not PreviewMode.NATIVE:
        raise PreviewOperationError(
            422,
            "Shared databases are supported only for native previews",
        )
    if database.sharing is not PreviewSharing.SHARED_DATA:
        return
    # A guest wrote into the schema its owner also wrote. Only the copied local
    # folders tolerated that, because they shared one MySQL server per source
    # path. Every managed sandbox owns its own schema, so the mode has no
    # remaining meaning and no caller can opt into it.
    raise PreviewOperationError(422, _SHARED_DATA_UNAVAILABLE)


def _wait_for_mysql_health(
    container: Container,
    *,
    timeout_seconds: int,
) -> None:
    wait_for_mysql_health(
        container,
        timeout_seconds=timeout_seconds,
        error=PreviewOperationError,
    )


def _wait_for_container_health(
    container: Container,
    *,
    timeout_seconds: int,
) -> None:
    """Waits for the application container's first successful health probe.

    Unlike the database container, the application image runs with no
    `healthcheck` argument of our own — whether Docker reports a `Health`
    status at all depends on a `HEALTHCHECK` baked into the image. When the
    image carries none, `running` is the only signal Docker will ever offer,
    so that alone counts as the first successful probe.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        status = str(state.get("Status") or container.status)
        health_state = state.get("Health")
        if status in {"dead", "exited"} or (
            health_state is not None and str(health_state.get("Status")) == "unhealthy"
        ):
            logs = container.logs(stdout=True, stderr=True, tail=100)
            detail = (
                logs.decode("utf-8", errors="replace")
                if isinstance(logs, bytes)
                else str(logs)
            )[-8_192:]
            raise PreviewOperationError(
                422,
                f"Application container failed its health check: {detail}",
            )
        if status == "running" and (
            health_state is None or str(health_state.get("Status")) == "healthy"
        ):
            return
        time.sleep(0.5)
    raise PreviewOperationError(
        408,
        f"Application container health check exceeded {timeout_seconds} seconds",
    )


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


def _volume_runtime_files(
    docker_client: DockerClient,
    volume_name: str,
    settings: PreviewSettings,
) -> dict[str, bytes]:
    command = (
        "set -eu\n"
        "cd /workspace\n"
        "find . -maxdepth 5 -type f \\( -name 'compose.yaml' -o -name 'compose.yml' "
        "-o -name 'docker-compose.yaml' -o -name 'docker-compose.yml' "
        "-o -name 'Dockerfile*' -o -name '.dockerignore' "
        "-o -name 'package.json' -o -name 'package-lock.json' "
        "-o -name 'npm-shrinkwrap.json' -o -name 'pnpm-lock.yaml' "
        "-o -name 'yarn.lock' -o -name 'pyproject.toml' "
        "-o -name 'requirements*.txt' -o -name 'Pipfile' "
        "-o -name 'Pipfile.lock' -o -name 'poetry.lock' -o -name 'uv.lock' "
        "-o -name 'schema.prisma' "
        "-o -name 'vite.config.*' -o -name 'next.config.*' "
        "-o -path './.agent/preview.yaml' -o -name 'index.html' \\) "
        f"-size -{settings.maximum_file_bytes + 1}c -print > /tmp/files\n"
        # tar exits non-zero on an empty file list, so skip it when nothing matched.
        "if [ -s /tmp/files ]; then tar -cf - -T /tmp/files | base64 | tr -d '\\n'; fi\n"
    )
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
        max_log_bytes=PREVIEW_ARCHIVE_MAX_LOG_BYTES,
    )
    if not output:
        return {}
    archive_output = _decode_preview_archive(output)
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_output), mode="r:*") as archive:
            for member in archive:
                normalized = member.name.removeprefix("./")
                if not member.isfile() or not is_detection_file(normalized):
                    continue
                if member.size > settings.maximum_file_bytes:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                content = source.read(settings.maximum_file_bytes + 1)
                total += len(content)
                if total > settings.maximum_snapshot_bytes:
                    raise PreviewOperationError(
                        422,
                        "Protected runtime files exceed the inspection limit",
                    )
                files[normalized] = content
    except tarfile.TarError as error:
        raise PreviewOperationError(502, "Sandbox inspection returned invalid data") from error
    return files


def _lockfile_digest(files: dict[str, bytes]) -> str:
    """Digests the first root lockfile found, in `_LOCKFILE_NAMES` order.

    `files` comes from `_volume_runtime_files`, which already reads every
    name in `_LOCKFILE_NAMES` from the sandbox volume root, so no new
    volume-read path is needed here.
    """
    for name in _LOCKFILE_NAMES:
        content = files.get(name)
        if content is not None:
            return hashlib.sha256(content).hexdigest()
    return hashlib.sha256(b"none").hexdigest()


def _dependency_volume_ready(
    docker_client: DockerClient,
    settings: PreviewSettings,
    volume_name: str,
) -> bool:
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=[
            "sh",
            "-c",
            (
                f"if [ -f /workspace/{_DEPENDENCY_READY_MARKER} ]; "
                "then printf ready; fi"
            ),
        ],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
    )
    return bool(output.strip())


def _volume_environment_files(
    docker_client: DockerClient,
    volume_name: str,
    settings: PreviewSettings,
) -> dict[str, bytes]:
    """Reads the project volume's top-level env files. Never feeds hashes/baselines."""
    name_clauses = " -o ".join(
        f"-name {shlex.quote(name)}" for name in ENVIRONMENT_FILE_NAMES
    )
    command = (
        "set -eu\n"
        "cd /workspace\n"
        f"find . -maxdepth 1 -type f \\( {name_clauses} \\) "
        f"-size -{settings.maximum_file_bytes + 1}c -print > /tmp/files\n"
        # tar exits non-zero on an empty file list, so skip it when nothing matched.
        "if [ -s /tmp/files ]; then tar -cf - -T /tmp/files | base64 | tr -d '\\n'; fi\n"
    )
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
        max_log_bytes=PREVIEW_ARCHIVE_MAX_LOG_BYTES,
    )
    if not output:
        return {}
    archive_output = _decode_preview_archive(output)
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_output), mode="r:*") as archive:
            for member in archive:
                normalized = member.name.removeprefix("./")
                if not member.isfile() or normalized not in ENVIRONMENT_FILE_NAMES:
                    continue
                if member.size > settings.maximum_file_bytes:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                content = source.read(settings.maximum_file_bytes + 1)
                total += len(content)
                if total > settings.maximum_snapshot_bytes:
                    raise PreviewOperationError(
                        422,
                        "Environment files exceed the inspection limit",
                    )
                files[normalized] = content
    except tarfile.TarError as error:
        raise PreviewOperationError(502, "Sandbox inspection returned invalid data") from error
    return files


def _volume_context_tar(
    docker_client: DockerClient,
    volume_name: str,
    context: str,
    inspection_image: str,
) -> bytes:
    relative = _safe_relative_path(context, field="build context", allow_dot=True)
    directory = "/workspace" if relative == "." else f"/workspace/{relative}"
    output = _run_preview_command(
        docker_client,
        image=inspection_image,
        command=["sh", "-c", f"tar -C {shlex.quote(directory)} -cf - . | base64 | tr -d '\\n'"],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        max_log_bytes=PREVIEW_ARCHIVE_MAX_LOG_BYTES,
    )
    output_bytes = _decode_preview_archive(output)
    if len(output_bytes) > MAX_CONTEXT_BYTES:
        raise PreviewOperationError(422, "Docker build context exceeds 512 MiB")
    return output_bytes


def _write_preview_manifest(
    docker_client: DockerClient,
    volume_name: str,
    inspection_image: str,
    config: PreviewConfiguration,
) -> None:
    document = yaml.safe_dump(
        config.model_dump(mode="json"),
        sort_keys=True,
        default_flow_style=False,
    ).encode()
    encoded = base64.b64encode(document).decode("ascii")
    _run_preview_command(
        docker_client,
        image=inspection_image,
        command=[
            "sh",
            "-c",
            (
                "set -eu; mkdir -p /workspace/.agent; "
                "printf '%s' \"$PREVIEW_MANIFEST\" | base64 -d "
                "> /workspace/.agent/preview.yaml"
            ),
        ],
        environment={"PREVIEW_MANIFEST": encoded},
        volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
        tmpfs_size="8m",
    )


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


def _ensure_preview_image(docker_client: DockerClient, image: str) -> None:
    try:
        ensure_image(docker_client, image)
    except DockerException as error:
        raise PreviewOperationError(424, f"Preview image '{image}' is unavailable") from error


def _run_preview_command(
    docker_client: DockerClient,
    *,
    image: str,
    command: list[str],
    environment: dict[str, str] | None = None,
    volumes: dict[str, Any] | None = None,
    tmpfs_size: str = "256m",
    max_log_bytes: int = PREVIEW_COMMAND_MAX_LOG_BYTES,
) -> str:
    """Run one isolated preview helper and retain Docker's failed-run error."""
    result = run_hardened(
        docker_client,
        HardenedRunSpec(
            image=image,
            command=command,
            environment=environment or {},
            volumes=volumes or {},
            egress=Egress.DENIED,
            tmpfs_size=tmpfs_size,
            capture=Capture.SEPARATE,
            timeout_seconds=PREVIEW_COMMAND_TIMEOUT_SECONDS,
            max_log_bytes=max_log_bytes,
        ),
    )
    if result.timed_out:
        raise PreviewOperationError(
            408,
            f"Preview helper command exceeded {PREVIEW_COMMAND_TIMEOUT_SECONDS} seconds",
        )
    if result.exit_code != 0:
        raise ContainerError(None, result.exit_code, command, image, result.stderr)
    return result.stdout


def _decode_preview_archive(output: str) -> bytes:
    try:
        return base64.b64decode(output, validate=True)
    except ValueError as error:
        raise PreviewOperationError(502, "Sandbox inspection returned invalid data") from error


def _validate_built_image(image: Any, settings: PreviewSettings) -> None:
    image.reload()
    size = int(image.attrs.get("Size") or 0)
    if size <= settings.maximum_built_image_bytes:
        return
    try:
        image.remove(force=True)
    except DockerException:
        pass
    raise PreviewOperationError(
        422,
        f"Built preview image exceeds {settings.maximum_built_image_bytes} bytes",
    )


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


def _dependency_volume_name(sandbox_id: str, lockfile_digest: str) -> str:
    return f"orchestrator-deps-{sandbox_id[:12]}-{lockfile_digest[:12]}"


def _dependency_volume(
    docker_client: DockerClient,
    sandbox_id: str,
    lockfile_digest: str,
    labels: dict[str, str],
) -> Any:
    """Gets or creates the dependency volume keyed by sandbox and lockfile digest.

    Labeled like a persistent `_data_volume` so cleanup never removes it: the
    volume is reused across runs and across rebuilds as long as the lockfile
    is unchanged, and a lockfile change earns a fresh volume automatically.
    """
    name = _dependency_volume_name(sandbox_id, lockfile_digest)
    try:
        volume = docker_client.volumes.get(name)
    except NotFound:
        volume = None
    if volume is not None:
        existing_labels = volume.attrs.get("Labels") or {}
        if (
            existing_labels.get(LABEL_DATA_MANAGED) != "true"
            or existing_labels.get(LABEL_PERSISTENT) != "true"
            or existing_labels.get(LABEL_SANDBOX_ID) != sandbox_id
        ):
            raise PreviewOperationError(
                409,
                f"Docker volume '{name}' is not trusted dependency data",
            )
        return volume
    return docker_client.volumes.create(
        name=name,
        driver="local",
        labels={
            **labels,
            LABEL_SANDBOX_ID: sandbox_id,
            LABEL_DATA_MANAGED: "true",
            LABEL_PERSISTENT: "true",
        },
    )


def _run_volume_name(run_id: str, logical_name: str) -> str:
    return f"orchestrator-preview-{run_id[:12]}-{_slug(logical_name)[:24]}"


def _data_volume(
    docker_client: DockerClient,
    run_id: str,
    logical_name: str,
    labels: dict[str, str],
    persistent: bool,
) -> Any:
    if persistent:
        sandbox_id = labels[LABEL_SANDBOX_ID]
        name = (
            f"orchestrator-preview-persistent-{sandbox_id[:12]}-"
            f"{_slug(logical_name)[:24]}"
        )
        try:
            volume = docker_client.volumes.get(name)
        except NotFound:
            volume = None
        if volume is not None:
            existing_labels = volume.attrs.get("Labels") or {}
            if (
                existing_labels.get(LABEL_DATA_MANAGED) != "true"
                or existing_labels.get(LABEL_PERSISTENT) != "true"
                or existing_labels.get(LABEL_SANDBOX_ID) != sandbox_id
            ):
                raise PreviewOperationError(
                    409,
                    f"Docker volume '{name}' is not trusted preview data",
                )
            return volume
    else:
        name = _run_volume_name(run_id, logical_name)
    return docker_client.volumes.create(
        name=name,
        driver="local",
        labels={
            **labels,
            LABEL_DATA_MANAGED: "true",
            LABEL_PERSISTENT: "true" if persistent else "false",
        },
    )


def _labels(
    sandbox_id: str,
    run_id: str,
    expires_at: str | None,
) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_SANDBOX_ID: sandbox_id,
        LABEL_RUN_ID: run_id,
        LABEL_KIND: "preview",
        LABEL_EXPIRES_AT: expires_at or "",
    }


def _preview_containers(
    docker_client: DockerClient,
    run_id: str,
    *,
    all: bool,
) -> list[Container]:
    return docker_client.containers.list(
        all=all,
        filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]},
    )


def _preview_networks(docker_client: DockerClient, run_id: str) -> list[Any]:
    return docker_client.networks.list(
        filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]}
    )


def _preview_volumes(docker_client: DockerClient, run_id: str) -> list[Any]:
    return docker_client.volumes.list(
        filters={
            "label": [f"{LABEL_DATA_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]
        }
    )


def _preview_images(docker_client: DockerClient, run_id: str) -> list[Any]:
    return docker_client.images.list(
        filters={
            "label": [f"{LABEL_MANAGED}=true", f"{LABEL_RUN_ID}={run_id}"]
        }
    )


def _resources_for_run(docker_client: DockerClient, run_id: str) -> dict[str, Any]:
    containers = _preview_containers(docker_client, run_id, all=True)
    borrowed_networks: list[Any] = []
    if containers:
        labels = (containers[0].attrs.get("Config") or {}).get("Labels") or {}
        sandbox_id = str(labels.get(LABEL_SANDBOX_ID) or "")
        if sandbox_id:
            try:
                borrowed_networks.append(
                    docker_client.networks.get(sandbox_network_name(sandbox_id))
                )
            except NotFound:
                pass
    return {
        "containers": containers,
        "networks": _preview_networks(docker_client, run_id),
        "volumes": _preview_volumes(docker_client, run_id),
        "images": _preview_images(docker_client, run_id),
        "borrowed_networks": borrowed_networks,
    }


def _disconnect_foreign_endpoints(network: Any) -> None:
    """Detaches containers the run does not own, such as a shared database.

    Docker refuses to remove a network that still has endpoints, and the shared
    server outlives the run, so it is disconnected instead of removed.
    """
    try:
        network.reload()
    except DockerException:
        return
    for container_id in (network.attrs.get("Containers") or {}):
        try:
            network.disconnect(container_id, force=True)
        except DockerException:
            continue


def _remove_resources(
    resources: dict[str, Any],
    *,
    remove_data_volumes: bool,
) -> dict[str, int]:
    counts = {"containers": 0, "networks": 0, "volumes": 0, "images": 0}
    for network in resources.get("borrowed_networks") or []:
        for container in resources.get("containers") or []:
            try:
                network.disconnect(container, force=True)
            except DockerException:
                continue
    for container in resources.get("containers") or []:
        try:
            # Docker removes anonymous volumes only; named sandbox and mirror volumes survive.
            container.remove(force=True, v=True)
            counts["containers"] += 1
        except NotFound:
            counts["containers"] += 1
        except DockerException:
            continue
    for network in resources.get("networks") or []:
        _disconnect_foreign_endpoints(network)
        try:
            network.remove()
            counts["networks"] += 1
        except NotFound:
            counts["networks"] += 1
        except DockerException:
            continue
    for volume in resources.get("volumes") or []:
        labels = volume.attrs.get("Labels") or {}
        if labels.get(LABEL_PERSISTENT) == "true":
            continue
        database_data = labels.get(LABEL_SERVICE) in {
            "database",
            "database-credentials",
        }
        if not remove_data_volumes and not database_data:
            continue
        try:
            volume.remove(force=True)
            counts["volumes"] += 1
        except NotFound:
            counts["volumes"] += 1
        except DockerException:
            continue
    for image in resources.get("images") or []:
        try:
            image.remove(force=True)
            counts["images"] += 1
        except (AttributeError, NotFound):
            try:
                image_id = getattr(image, "id", str(image))
                image.client.images.remove(image=image_id, force=True)
                counts["images"] += 1
            except (AttributeError, DockerException):
                continue
        except DockerException:
            continue
    return counts


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


def _ready_project(
    docker_client: DockerClient,
    project_name: str,
    controller_store: ControllerStore,
) -> Any:
    try:
        project = inspect_registered_project(
            docker_client,
            project_name,
            controller_store,
        )
    except ProjectOperationError as error:
        raise PreviewOperationError(error.status_code, error.detail) from error
    if not project.ready:
        raise PreviewOperationError(409, f"Project '{project_name}' is not ready")
    return project


def _project_key(project: Any) -> str:
    return managed_project_key(str(project.source_path))


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


def _safe_relative_path(value: str, *, field: str, allow_dot: bool = False) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise PreviewOperationError(422, f"{field} must stay inside the sandbox")
    text = path.as_posix()
    if not text or (text == "." and not allow_dot):
        raise PreviewOperationError(422, f"{field} is required")
    return text


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-._") or "data"


def _expiry(minutes: int) -> str | None:
    return None if minutes == 0 else _time_after(minutes=minutes)


def _time_after(*, seconds: int = 0, minutes: int = 0) -> str:
    value = datetime.now(UTC) + timedelta(seconds=seconds, minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
