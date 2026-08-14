"""Sandbox manifest API."""

import json
from dataclasses import replace
from typing import Annotated

from docker.client import DockerClient
from docker.errors import DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, field_validator

from app.controller.store import (
    ControllerStore,
    SandboxAdmissionError,
    get_controller_store,
)
from app.docker_client import get_docker_client
from app.projects.remote import normalize_remote_url, project_id_for_remote
from app.sandboxes.manifest import SandboxManifest, read_manifest, write_manifest
from app.sandboxes.lifecycle import (
    drain_sandbox_writers,
    lifecycle_conflict_detail,
    lifecycle_lease,
    project_mirror_lock,
)
from app.sandboxes.database import (
    SandboxDatabaseError,
    SandboxMigrationError,
    drop_sandbox_database,
    provision_sandbox_database,
    sandbox_database_runtime,
)
from app.sandboxes.engine_detection import (
    EngineDetection,
    NO_DATABASE,
    discover_engine,
    discover_schema_baseline_files,
    normalize_confirmable_engine,
)
from app.sandboxes.mirror import (
    ensure_project_mirror,
    ensure_workspace_import,
    validate_project_mirror,
    validate_workspace_import,
    verify_workspace_identity,
)
from app.sandboxes.git import (
    count_mirror_staleness,
    create_workspace_safety_ref,
    describe_git_failure,
    fetch_canonical_mirror,
    mirror_base_commit,
    require_clean_workspace,
    restore_workspace_safety_ref,
    sync_workspace_from_mirror,
)
from app.sandboxes.publish import (
    GitHubApiError,
    PublishError,
    discover_or_create_pull_request,
    publish_reviewed_feature,
    reviewed_target,
)
from app.sandboxes.naming import (
    database_name,
    db_data_volume,
    feature_branch,
    mirror_volume,
    is_shared_infrastructure,
    orphan_ownership_sandbox_id,
    sandbox_id_for,
    validate_feature_key,
    validate_ownership,
    workspace_volume,
)
from app.sandboxes.orphans import (
    parse_orphan_resource_key,
    resource_is_claimed,
)
from app.previews.config import get_preview_settings


router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


class CreateSandboxRequest(BaseModel):
    remote_url: str | None = None
    feature_key: str
    feature_title: str | None = None
    agent_provider: str | None = None
    stop_blocking_previews: bool = False
    engine_confirmation: "EngineConfirmationRequest | None" = None

    @field_validator("feature_key")
    @classmethod
    def validate_requested_feature_key(cls, value: str) -> str:
        try:
            return validate_feature_key(value)
        except ValueError as error:
            raise ValueError(str(error)) from error

    @field_validator("remote_url")
    @classmethod
    def normalize_requested_remote(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_remote_url(value)
        except ValueError as error:
            raise ValueError(str(error)) from error


class SandboxResponse(BaseModel):
    sandbox_id: str
    project_id: str
    lifecycle_version: str | None = None
    feature_key: str | None = None
    feature_title: str | None = None
    desired_state: str | None = None
    lifecycle_status: str | None = None
    base_ref: str | None = None
    feature_branch: str | None = None
    created_base_commit: str | None = None
    current_base_commit: str | None = None
    pending_base_commit: str | None = None
    db_engine: str | None = None
    db_name: str | None = None
    db_data_volume: str | None = None
    schema_baseline_hash: str | None = None
    remote_url: str | None = None


class SandboxListResponse(BaseModel):
    count: int
    sandboxes: list[SandboxResponse]


class DestroySandboxResponse(BaseModel):
    sandbox_id: str
    destroyed_at: str
    reason: str


class OrphanResourceResponse(BaseModel):
    resource: str
    kind: str
    name: str
    reported_at: str


class OrphanResourcesResponse(BaseModel):
    count: int
    resources: list[OrphanResourceResponse]


class RemoveOrphanResourceResponse(BaseModel):
    resource: str
    removed: bool


class SandboxStalenessResponse(BaseModel):
    behind_count: int | None
    base_ref: str
    current_base_commit: str
    mirror_fetched_at: str | None
    stale_answer: bool
    fetch_failure_reason: str | None = None


class EngineConfirmationRequest(BaseModel):
    engine: str
    migrate_commands: list[str] = []
    seed_commands: list[str] = []
    commands_source: dict[str, str] = {}
    actor: str

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, value: str) -> str:
        engine = normalize_confirmable_engine(value)
        if engine is None:
            raise ValueError("engine must be mysql, postgres, sqlite, or none")
        return engine

    @field_validator("migrate_commands", "seed_commands")
    @classmethod
    def validate_commands(cls, value: list[str]) -> list[str]:
        if any(not command.strip() for command in value):
            raise ValueError("commands must not be blank")
        return value

    @field_validator("commands_source")
    @classmethod
    def validate_command_sources(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"prisma", "package_json", "makefile", "manual"}
        if any(key not in {"migrate", "seed"} or source not in allowed for key, source in value.items()):
            raise ValueError("commands_source uses migrate or seed with a known source")
        return value


class EngineDetectionResponse(BaseModel):
    sandbox_id: str
    signals: list[dict[str, object]]
    proposed_engine: str | None
    confirmed_engine: str | None
    migrate_commands: list[str]
    seed_commands: list[str]
    commands_source: dict[str, str]
    detected_at_commit: str
    actor: str | None
    confirmed_at: str | None


class ResetDatabaseRequest(BaseModel):
    stop_blocking_preview: bool = False


class SyncSandboxRequest(BaseModel):
    """Explicit consent is required before sync stops a live preview."""

    stop_blocking_preview: bool = False


class PublishSandboxRequest(BaseModel):
    """Explicit consent is required before publish stops a live preview."""

    stop_blocking_preview: bool = False


class EngineSyncReport(BaseModel):
    confirmed_engine: str | None
    detected_engine: str | None
    mismatch: bool
    detection_error: str | None = None


class SyncSandboxResponse(SandboxResponse):
    operation_id: str
    safety_ref: str
    strategy: str
    engine_report: EngineSyncReport


class PublishSandboxResponse(BaseModel):
    sandbox_id: str
    operation_id: str
    remote_branch: str
    last_pushed_commit: str
    remote_branch_sha: str
    pushed: bool
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    pr_merged_at: str | None = None


class SandboxPublicationResponse(BaseModel):
    sandbox_id: str
    remote_branch: str
    last_pushed_commit: str | None = None
    remote_branch_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    pr_merged_at: str | None = None
    last_error: str | None = None
    updated_at: str


@router.post("", response_model=SandboxResponse, status_code=status.HTTP_201_CREATED)
def create_or_resolve_sandbox(
    request: CreateSandboxRequest,
    response: Response,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Provision the Phase 4 Git resources for a deterministic v1 sandbox."""
    if not request.remote_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Sandbox creation requires a Git remote.",
        )
    project_id = project_id_for_remote(request.remote_url)
    sandbox_id = sandbox_id_for(project_id, request.feature_key)
    project = controller_store.register_v1_project(
        project_id=project_id,
        remote_url=request.remote_url,
        default_branch="",
        mirror_volume=mirror_volume(project_id),
        created_at="",
    )
    sandbox, created = controller_store.register_v1_sandbox(
        sandbox_id=sandbox_id,
        project_id=str(project["id"]),
        project_name=str(project["remote_url"]),
        volume_name=workspace_volume(sandbox_id),
        created_at="",
    )
    try:
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "create",
            docker_client=docker_client,
            stop_blocking_previews=request.stop_blocking_previews,
        ):
            if created:
                write_manifest(
                    controller_store,
                    SandboxManifest(
                        sandbox_id=sandbox_id,
                        lifecycle_version="v1",
                        feature_key=request.feature_key,
                        feature_title=request.feature_title,
                        desired_state="active",
                        lifecycle_status="creating",
                        feature_branch=feature_branch(request.feature_key),
                        agent_provider=request.agent_provider,
                        operation="create",
                        operation_phase="mirror",
                    ),
                )
            if not created:
                try:
                    # Even inspection validates shared mirror state, so it
                    # takes the project lock after the sandbox lease.
                    with project_mirror_lock(controller_store, project_id, "create"):
                        validate_project_mirror(docker_client, project_id=project_id)
                    validate_workspace_import(docker_client, sandbox_id=sandbox_id)
                except (ValueError, RuntimeError) as error:
                    raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
                response.status_code = status.HTTP_200_OK
                return _sandbox_response(controller_store, sandbox)

            try:
                git_image = get_preview_settings().git_image
                # Fixed global order: sandbox lease, then project mirror lock.
                # The lock ends before the clone, so separate sandbox creates
                # only serialize their shared fetch.
                with project_mirror_lock(controller_store, project_id, "create"):
                    mirror = ensure_project_mirror(
                        docker_client,
                        image=git_image,
                        project_id=project_id,
                        remote_url=str(project["remote_url"]),
                    )
                controller_store.set_v1_project_mirror(
                    project_id=project_id,
                    default_branch=mirror.default_branch,
                    mirror_volume=mirror.volume_name,
                )
                manifest = read_manifest(controller_store, sandbox_id)
                if manifest is None:
                    raise RuntimeError("v1 sandbox manifest disappeared during create")
                pinned_ref = f"refs/heads/{mirror.default_branch}"
                manifest = replace(
                    manifest,
                    operation_phase="workspace",
                    base_ref=pinned_ref,
                    created_base_commit=mirror.commit,
                    current_base_commit=mirror.commit,
                )
                write_manifest(controller_store, manifest)
                controller_store.record_sandbox_resource(
                    sandbox_id, kind="volume", name=workspace_volume(sandbox_id)
                )
                ensure_workspace_import(
                    docker_client,
                    image=git_image,
                    sandbox_id=sandbox_id,
                    project_id=project_id,
                    mirror=mirror,
                    feature_branch=manifest.feature_branch
                    or feature_branch(request.feature_key),
                )
                manifest = replace(manifest, operation_phase="cloned")
                write_manifest(controller_store, manifest)
                detection = discover_engine(
                    docker_client,
                    image=git_image,
                    volume_name=workspace_volume(sandbox_id),
                )
                if detection.tracked_database_paths:
                    paths = ", ".join(detection.tracked_database_paths)
                    raise ValueError(
                        "Project tracks database file(s): "
                        f"{paths}. Move the database out of Git before creating a sandbox."
                    )
                detection_row = controller_store.record_sandbox_engine_detection(
                    sandbox_id=sandbox_id,
                    signals=[signal.as_dict() for signal in detection.signals],
                    proposed_engine=detection.proposed_engine,
                    migrate_commands=detection.migrate_commands,
                    seed_commands=detection.seed_commands,
                    commands_source=detection.commands_source,
                    detected_at_commit=manifest.created_base_commit or "",
                )
                if request.engine_confirmation is not None:
                    _confirm_engine_snapshot(
                        controller_store,
                        sandbox_id=sandbox_id,
                        request=request.engine_confirmation,
                        detection=detection_row,
                    )
                    _complete_database_provision(
                        docker_client,
                        controller_store,
                        sandbox_id=sandbox_id,
                        operation="create",
                        rebuild=False,
                    )
                else:
                    # A human decision can take an arbitrary time. Leaving
                    # this context releases the lease before we return.
                    write_manifest(
                        controller_store,
                        replace(
                            manifest,
                            lifecycle_status="awaiting_engine_confirmation",
                            operation="create",
                            operation_phase="awaiting_engine_confirmation",
                        ),
                    )
            except ValueError as error:
                raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
            except RuntimeError as error:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    str(error),
                ) from error
            except SandboxDatabaseError as error:
                raise HTTPException(error.status_code, error.detail) from error
            return _sandbox_response(controller_store, sandbox)
    except SandboxAdmissionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            lifecycle_conflict_detail(error),
        ) from error


@router.get("", response_model=SandboxListResponse)
def list_sandboxes(
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxListResponse:
    sandboxes = [
        _sandbox_response(controller_store, sandbox)
        for sandbox in controller_store.sandboxes()
    ]
    return SandboxListResponse(count=len(sandboxes), sandboxes=sandboxes)


@router.get("/orphans", response_model=OrphanResourcesResponse)
def list_orphan_resources(
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> OrphanResourcesResponse:
    """Read the startup report without claiming a lease or a mirror lock."""
    resources = []
    for resource in controller_store.unexpected_resources():
        try:
            orphan = parse_orphan_resource_key(str(resource["resource"]))
        except (KeyError, ValueError):
            continue
        if not resource_is_claimed(controller_store, orphan):
            resources.append(resource)
    return OrphanResourcesResponse(count=len(resources), resources=resources)


@router.post("/orphans/{resource}/remove", response_model=RemoveOrphanResourceResponse)
def remove_orphan_resource(
    resource: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> RemoveOrphanResourceResponse:
    """Remove one operator-selected orphan after checking live manifest ownership."""
    try:
        orphan = parse_orphan_resource_key(resource)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    collection = getattr(docker_client, f"{orphan.kind}s")
    try:
        docker_resource = collection.get(orphan.name)
    except NotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Orphan resource not found") from error
    try:
        if is_shared_infrastructure(docker_resource):
            raise ValueError("shared infrastructure cannot be removed as an orphan")
        # A name is only a discovery hint. Removal requires the complete v1
        # ownership-label shape, which prevents deleting an unrelated sbx-* resource.
        orphan_ownership_sandbox_id(docker_resource)
        if resource_is_claimed(controller_store, orphan):
            raise ValueError("resource is now claimed by a sandbox manifest")
        _remove_manifest_resource(docker_resource, orphan.kind)
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except DockerException as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
    return RemoveOrphanResourceResponse(resource=orphan.key, removed=True)


@router.get("/{sandbox_id}", response_model=SandboxResponse)
def get_sandbox(
    sandbox_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
    return _sandbox_response(controller_store, sandbox)


@router.get("/{sandbox_id}/staleness", response_model=SandboxStalenessResponse)
def get_sandbox_staleness(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxStalenessResponse:
    """Fetch the shared mirror, then report this sandbox's informational lag."""
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_staleness(sandbox, sandbox_id)
    assert sandbox is not None
    project = controller_store.project(str(sandbox["project_id"]))
    if project is None or not project.get("mirror_volume"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no project mirror; recreate it explicitly to use v1.",
        )
    base_ref = _required_staleness_value(sandbox, "base_ref", sandbox_id)
    current_base_commit = _required_staleness_value(
        sandbox, "current_base_commit", sandbox_id
    )
    mirror_name = str(project["mirror_volume"])
    git_image = get_preview_settings().git_image
    fetch_failure_reason: str | None = None

    # Staleness is sandbox read-only. It intentionally takes no lifecycle
    # lease, so an active agent, task, or preview cannot block inspection.
    # It locks only the shared mirror while canonical fetch mutates its refs.
    try:
        with project_mirror_lock(controller_store, str(project["id"]), "staleness"):
            try:
                fetch_canonical_mirror(
                    docker_client,
                    image=git_image,
                    mirror_volume=mirror_name,
                    ensure_image=True,
                )
            except Exception as error:
                fetch_failure_reason = str(error)
                project = controller_store.project(str(sandbox["project_id"])) or project
    except SandboxAdmissionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            lifecycle_conflict_detail(error),
        ) from error
    if fetch_failure_reason is None:
        project = controller_store.record_v1_project_mirror_fetch(
            project_id=str(project["id"])
        )

    try:
        behind_count = count_mirror_staleness(
            docker_client,
            image=git_image,
            mirror_volume=mirror_name,
            current_base_commit=current_base_commit,
            base_ref=base_ref,
            ensure_image=True,
        )
    except Exception as error:
        if fetch_failure_reason is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error
        fetch_failure_reason = f"{fetch_failure_reason}; last known mirror state is unavailable: {error}"
        behind_count = None

    return SandboxStalenessResponse(
        behind_count=behind_count,
        base_ref=base_ref,
        current_base_commit=current_base_commit,
        mirror_fetched_at=_optional_string(project.get("mirror_fetched_at")),
        stale_answer=fetch_failure_reason is not None,
        fetch_failure_reason=fetch_failure_reason,
    )


@router.post(
    "/{sandbox_id}/sync",
    response_model=SyncSandboxResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def sync_sandbox(
    sandbox_id: str,
    request: SyncSandboxRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SyncSandboxResponse:
    """Explicitly bring one clean v1 workspace forward from its local mirror."""
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_sync(sandbox, sandbox_id)
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "Sandbox can sync only from ready")
    project = controller_store.project(str(sandbox["project_id"]))
    if project is None or not project.get("mirror_volume"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no project mirror; recreate it explicitly to use v1.",
        )
    base_ref = _required_sync_value(sandbox, "base_ref", sandbox_id)
    current_base_commit = _required_sync_value(
        sandbox, "current_base_commit", sandbox_id
    )
    if not manifest.feature_branch:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no feature branch; recreate it explicitly to use v1.",
        )

    git_image = get_preview_settings().git_image
    mirror_name = str(project["mirror_volume"])
    workspace_name = workspace_volume(sandbox_id)
    try:
        # The lease is always taken before the project mirror lock.  The clean
        # check happens after admission, so a writer cannot change Git between
        # the check and the safety ref.
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "sync",
            docker_client=docker_client,
            stop_blocking_previews=request.stop_blocking_preview,
        ) as lease:
            if lease is None:  # _require_v1_sync above keeps this defensive.
                raise RuntimeError("managed sandbox did not acquire a lifecycle lease")
            operation_id = str(lease["operation_id"])
            safety_ref = f"refs/orchestrator/safety/{operation_id}"

            # This is deliberately first. A dirty worktree has no safe sync
            # semantics, and must leave both Git and manifest untouched.
            try:
                require_clean_workspace(
                    docker_client,
                    image=git_image,
                    workspace_volume=workspace_name,
                    ensure_image=True,
                )
            except Exception as error:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Sandbox workspace is dirty; sync refused before changes: {error}",
                ) from error

            create_workspace_safety_ref(
                docker_client,
                image=git_image,
                workspace_volume=workspace_name,
                safety_ref=safety_ref,
                ensure_image=True,
            )

            # The only network-enabled step is the existing canonical fetch.
            # The sandbox worktree never receives its credentials or a remote.
            with project_mirror_lock(controller_store, str(project["id"]), "sync"):
                fetch_canonical_mirror(
                    docker_client,
                    image=git_image,
                    mirror_volume=mirror_name,
                    ensure_image=True,
                )
            controller_store.record_v1_project_mirror_fetch(
                project_id=str(project["id"])
            )
            pending_base_commit = mirror_base_commit(
                docker_client,
                image=git_image,
                mirror_volume=mirror_name,
                base_ref=base_ref,
                ensure_image=True,
            )

            # Intent is not evidence. An observed open PR preserves its branch
            # history with a merge; every other case uses the pre-PR rebase path.
            sync_strategy = _sync_strategy(controller_store, sandbox_id)
            syncing = replace(
                manifest,
                lifecycle_status="syncing",
                operation="sync",
                operation_phase="git_sync",
                pending_base_commit=pending_base_commit,
                last_error=None,
            )
            write_manifest(controller_store, syncing)
            try:
                sync_workspace_from_mirror(
                    docker_client,
                    image=git_image,
                    mirror_volume=mirror_name,
                    workspace_volume=workspace_name,
                    base_ref=base_ref,
                    pending_base_commit=pending_base_commit,
                    strategy=sync_strategy,
                    ensure_image=True,
                )
            except Exception as sync_error:
                try:
                    restore_workspace_safety_ref(
                        docker_client,
                        image=git_image,
                        workspace_volume=workspace_name,
                        safety_ref=safety_ref,
                        ensure_image=True,
                    )
                except Exception as restore_error:
                    detail = (
                        f"Git sync failed: {sync_error}. The controller could not restore "
                        f"safety ref '{safety_ref}': {restore_error}"
                    )
                else:
                    detail = (
                        f"Git sync failed and Git was restored from safety ref "
                        f"'{safety_ref}': {sync_error}"
                    )
                failed = read_manifest(controller_store, sandbox_id)
                if failed is not None:
                    write_manifest(
                        controller_store,
                        replace(
                            failed,
                            lifecycle_status="ready",
                            operation="sync",
                            operation_phase="git_restored",
                            current_base_commit=current_base_commit,
                            pending_base_commit=None,
                            last_error=detail,
                        ),
                    )
                raise HTTPException(status.HTTP_409_CONFLICT, detail) from sync_error

            # This runner reads only the approved controller snapshot. It does
            # not read preview configuration or infer commands from the new tree.
            _complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="sync",
                rebuild=True,
            )
            engine_report = _sync_engine_report(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                image=git_image,
            )
            refreshed = controller_store.sandbox(sandbox_id)
            if refreshed is None:
                raise RuntimeError("sandbox disappeared after sync")
            response = _sandbox_response(controller_store, refreshed)
            return SyncSandboxResponse(
                **response.model_dump(),
                operation_id=operation_id,
                safety_ref=safety_ref,
                strategy=sync_strategy,
                engine_report=engine_report,
            )
    except SandboxAdmissionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            lifecycle_conflict_detail(error),
        ) from error
    except SandboxDatabaseError as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (DockerException, RuntimeError, ValueError) as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error


@router.post(
    "/{sandbox_id}/publish",
    response_model=PublishSandboxResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def publish_sandbox(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
    request: PublishSandboxRequest = PublishSandboxRequest(),
) -> PublishSandboxResponse:
    """Push one reviewed branch, then discover or create and verify its PR."""
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_publish(sandbox, sandbox_id)
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "Sandbox can publish only from ready")
    if not manifest.feature_branch:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no feature branch; recreate it explicitly to publish.",
        )
    project = controller_store.project(str(sandbox["project_id"]))
    if project is None or not project.get("remote_url") or not project.get("mirror_volume"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no project mirror or remote; recreate it explicitly to publish.",
        )
    prior_publication = controller_store.sandbox_publication(sandbox_id) or {}
    remote_branch = _optional_string(prior_publication.get("remote_branch")) or (
        manifest.remote_branch or manifest.feature_branch
    )
    base_ref = _required_sync_value(sandbox, "base_ref", sandbox_id)
    workspace_name = workspace_volume(sandbox_id)
    mirror_name = str(project["mirror_volume"])
    preview_settings = get_preview_settings()

    try:
        # This reads the stored review only. It must run before the lifecycle
        # lease, which can stop a requested blocking preview.
        reviewed_target(
            controller_store,
            sandbox_id=sandbox_id,
            feature_branch=manifest.feature_branch,
        )
        # Lock order is fixed: sandbox lifecycle lease, then project mirror
        # lock. The mirror lock covers both local staging and network push.
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "publish",
            docker_client=docker_client,
            stop_blocking_previews=request.stop_blocking_preview,
        ) as lease:
            if lease is None:
                raise RuntimeError("managed sandbox did not acquire a lifecycle lease")
            operation_id = str(lease["operation_id"])
            write_manifest(
                controller_store,
                replace(
                    manifest,
                    lifecycle_status="publishing",
                    operation="publish",
                    operation_phase="pushing",
                    last_error=None,
                ),
            )
            try:
                with project_mirror_lock(
                    controller_store,
                    str(project["id"]),
                    "publish",
                    operation_id=operation_id,
                ):
                    outcome = publish_reviewed_feature(
                        docker_client,
                        store=controller_store,
                        preview_settings=preview_settings,
                        sandbox_id=sandbox_id,
                        workspace_volume=workspace_name,
                        mirror_volume=mirror_name,
                        feature_branch=manifest.feature_branch,
                        remote_branch=remote_branch,
                    )
            except Exception as error:
                failure_message = describe_git_failure(error)
                prior = controller_store.sandbox_publication(sandbox_id) or {}
                controller_store.record_sandbox_publication(
                    sandbox_id=sandbox_id,
                    remote_branch=remote_branch,
                    last_pushed_commit=_optional_string(prior.get("last_pushed_commit")),
                    remote_branch_sha=_optional_string(prior.get("remote_branch_sha")),
                    last_error=failure_message,
                )
                failed = read_manifest(controller_store, sandbox_id)
                if failed is not None:
                    write_manifest(
                        controller_store,
                        replace(
                            failed,
                            lifecycle_status="ready",
                            operation="publish",
                            operation_phase="pushing",
                            last_error=failure_message,
                        ),
                    )
                raise
            publication = controller_store.record_sandbox_publication(
                sandbox_id=sandbox_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                last_error=None,
            )
            pushed = read_manifest(controller_store, sandbox_id)
            if pushed is None:
                raise RuntimeError("sandbox manifest disappeared during publish")
            write_manifest(
                controller_store,
                replace(
                    pushed,
                    lifecycle_status="publishing",
                    operation="publish",
                    operation_phase="pushed",
                    last_error=None,
                ),
            )
            pushed = read_manifest(controller_store, sandbox_id)
            if pushed is None:
                raise RuntimeError("sandbox manifest disappeared during publish")
            write_manifest(
                controller_store,
                replace(
                    pushed,
                    lifecycle_status="publishing",
                    operation="publish",
                    operation_phase="pr_pending",
                    last_error=None,
                ),
            )
            try:
                pull_request = discover_or_create_pull_request(
                    remote_url=str(project["remote_url"]),
                    remote_branch=outcome.remote_branch,
                    base_branch=_base_branch(base_ref),
                    title=manifest.feature_title or manifest.feature_key or outcome.remote_branch,
                )
            except (GitHubApiError, PublishError) as error:
                # Git and GitHub are independent systems. The successful push
                # remains observed evidence; the safe error text is retryable.
                controller_store.record_sandbox_publication(
                    sandbox_id=sandbox_id,
                    remote_branch=outcome.remote_branch,
                    last_pushed_commit=outcome.last_pushed_commit,
                    remote_branch_sha=outcome.remote_branch_sha,
                    last_error=str(error),
                )
                failed = read_manifest(controller_store, sandbox_id)
                if failed is not None:
                    write_manifest(
                        controller_store,
                        replace(
                            failed,
                            lifecycle_status="ready",
                            operation="publish",
                            operation_phase="pr_pending",
                            last_error=str(error),
                        ),
                    )
                raise PublishError(424, str(error)) from error
            publication = controller_store.record_sandbox_publication(
                sandbox_id=sandbox_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                pr_number=pull_request.number,
                pr_url=pull_request.url,
                pr_state=pull_request.state,
                pr_merged_at=pull_request.merged_at,
                last_error=None,
            )
            completed = read_manifest(controller_store, sandbox_id)
            if completed is None:
                raise RuntimeError("sandbox manifest disappeared during publish")
            write_manifest(
                controller_store,
                replace(
                    completed,
                    lifecycle_status="ready",
                    operation="publish",
                    operation_phase="published",
                    last_error=None,
                ),
            )
            return PublishSandboxResponse(
                sandbox_id=sandbox_id,
                operation_id=operation_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                pushed=outcome.pushed,
                pr_number=publication.get("pr_number"),
                pr_url=_optional_string(publication.get("pr_url")),
                pr_state=_optional_string(publication.get("pr_state")),
                pr_merged_at=_optional_string(publication.get("pr_merged_at")),
            )
    except SandboxAdmissionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            lifecycle_conflict_detail(error),
        ) from error
    except PublishError as error:
        raise HTTPException(error.status_code, error.detail) from error
    except (DockerException, RuntimeError, ValueError) as error:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, describe_git_failure(error)) from error


@router.get("/{sandbox_id}/publication", response_model=SandboxPublicationResponse)
def get_sandbox_publication(
    sandbox_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxPublicationResponse:
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_publish(sandbox, sandbox_id)
    publication = controller_store.sandbox_publication(sandbox_id)
    if publication is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox has no publication record")
    return SandboxPublicationResponse(**publication)


@router.get("/{sandbox_id}/engine", response_model=EngineDetectionResponse)
def get_engine_detection(
    sandbox_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> EngineDetectionResponse:
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_engine_lifecycle(sandbox, sandbox_id)
    detection = controller_store.sandbox_engine_detection(sandbox_id)
    if detection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Engine detection is not available")
    return _engine_detection_response(detection)


@router.post("/{sandbox_id}/confirm-engine", response_model=SandboxResponse)
def confirm_engine(
    sandbox_id: str,
    request: EngineConfirmationRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Freeze a human-approved engine and resume the creation lifecycle."""
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_engine_lifecycle(sandbox, sandbox_id)
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status != "awaiting_engine_confirmation":
        raise HTTPException(status.HTTP_409_CONFLICT, "Sandbox is not awaiting engine confirmation")
    detection = controller_store.sandbox_engine_detection(sandbox_id)
    if detection is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Sandbox has no engine detection to confirm")
    try:
        # This is intentionally a fresh lifecycle lease. The create lease was
        # released before the human received the proposal.
        with lifecycle_lease(controller_store, sandbox_id, "confirm-engine", docker_client=docker_client):
            _confirm_engine_snapshot(
                controller_store,
                sandbox_id=sandbox_id,
                request=request,
                detection=detection,
            )
            current = read_manifest(controller_store, sandbox_id)
            if current is None:
                raise RuntimeError("v1 sandbox manifest disappeared during engine confirmation")
            write_manifest(
                controller_store,
                replace(
                    current,
                    lifecycle_status="creating",
                    db_engine=request.engine,
                    operation="confirm-engine",
                    operation_phase="database_provisioning",
                    last_error=None,
                ),
            )
            _complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="confirm-engine",
                rebuild=False,
            )
    except SandboxAdmissionError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, lifecycle_conflict_detail(error)) from error
    except SandboxDatabaseError as error:
        raise HTTPException(error.status_code, error.detail) from error
    return _sandbox_response(controller_store, sandbox)


@router.post("/{sandbox_id}/reset-db", response_model=SandboxResponse)
def reset_database(
    sandbox_id: str,
    request: ResetDatabaseRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Drop and rebuild from the stored, human-approved command snapshot."""
    sandbox = controller_store.sandbox(sandbox_id)
    _require_v1_engine_lifecycle(sandbox, sandbox_id)
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status not in {"ready", "database_failed"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Sandbox database can reset only from ready or database_failed",
        )
    if manifest.db_engine == NO_DATABASE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no database to reset",
        )
    try:
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "reset-db",
            docker_client=docker_client,
            stop_blocking_previews=request.stop_blocking_preview,
        ):
            write_manifest(
                controller_store,
                replace(
                    manifest,
                    operation="reset-db",
                    operation_phase="database_rebuilding",
                    last_error=None,
                ),
            )
            _complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="reset-db",
                rebuild=True,
            )
    except SandboxAdmissionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            lifecycle_conflict_detail(error),
        ) from error
    except SandboxDatabaseError as error:
        raise HTTPException(error.status_code, error.detail) from error
    return _sandbox_response(controller_store, sandbox)


@router.post("/{sandbox_id}/resume", response_model=SandboxResponse)
def resume_sandbox(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Converge safe missing v1 resources without replacing workspace state."""
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise HTTPException(status.HTTP_409_CONFLICT, "Legacy sandboxes do not support resume")
    if sandbox.get("desired_state") != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Destroyed sandboxes cannot resume")
    try:
        with lifecycle_lease(controller_store, sandbox_id, "resume", docker_client=docker_client):
            manifest = read_manifest(controller_store, sandbox_id)
            project = controller_store.project(str(sandbox["project_id"]))
            if manifest is None or project is None:
                raise RuntimeError("sandbox manifest or project is missing")
            # The mirror is shared. Validate it under the project lock, but do
            # not retain that lock while inspecting or repairing the workspace.
            try:
                with project_mirror_lock(controller_store, str(project["id"]), "resume"):
                    validate_project_mirror(docker_client, project_id=str(project["id"]))
            except ValueError as error:
                raise RuntimeError(f"unsafe mirror ownership inconsistency: {error}") from error
            try:
                validate_workspace_import(docker_client, sandbox_id=sandbox_id)
                if not manifest.feature_branch:
                    raise RuntimeError("workspace feature branch is missing from the manifest")
                verify_workspace_identity(
                    docker_client,
                    image=get_preview_settings().git_image,
                    sandbox_id=sandbox_id,
                    feature_branch=manifest.feature_branch,
                )
            except ValueError as error:
                raise RuntimeError(f"unsafe workspace ownership inconsistency: {error}") from error
            except RuntimeError as error:
                if "workspace is missing" not in str(error):
                    raise
                # A missing workspace is safe to recreate.  It has no worktree
                # to preserve.  We use the immutable original base, never the
                # latest mirror head.
                from app.sandboxes.mirror import MirrorPin

                if not manifest.created_base_commit or not manifest.feature_branch:
                    raise RuntimeError("workspace is missing and the immutable clone identity is absent")
                controller_store.record_sandbox_resource(
                    sandbox_id, kind="volume", name=workspace_volume(sandbox_id)
                )
                ensure_workspace_import(
                    docker_client,
                    image=get_preview_settings().git_image,
                    sandbox_id=sandbox_id,
                    project_id=str(project["id"]),
                    mirror=MirrorPin(
                        mirror_volume(str(project["id"])),
                        str(project.get("default_branch") or "main"),
                        manifest.created_base_commit,
                    ),
                    feature_branch=manifest.feature_branch,
                )
            if manifest.lifecycle_status == "awaiting_engine_confirmation":
                return _sandbox_response(controller_store, sandbox)
            detection = controller_store.sandbox_engine_detection(sandbox_id)
            if not detection or not detection.get("confirmed_engine"):
                if detection is None:
                    detected = discover_engine(
                        docker_client,
                        image=get_preview_settings().git_image,
                        volume_name=workspace_volume(sandbox_id),
                    )
                    if detected.tracked_database_paths:
                        paths = ", ".join(detected.tracked_database_paths)
                        raise ValueError(
                            "Project tracks database file(s): "
                            f"{paths}. Move the database out of Git before creating a sandbox."
                        )
                    detection = controller_store.record_sandbox_engine_detection(
                        sandbox_id=sandbox_id,
                        signals=[signal.as_dict() for signal in detected.signals],
                        proposed_engine=detected.proposed_engine,
                        migrate_commands=detected.migrate_commands,
                        seed_commands=detected.seed_commands,
                        commands_source=detected.commands_source,
                        detected_at_commit=manifest.created_base_commit or "",
                    )
                refreshed = read_manifest(controller_store, sandbox_id)
                if refreshed is None:
                    raise RuntimeError("v1 sandbox manifest disappeared during resume")
                write_manifest(
                    controller_store,
                    replace(
                        refreshed,
                        lifecycle_status="awaiting_engine_confirmation",
                        operation="resume",
                        operation_phase="awaiting_engine_confirmation",
                        last_error=None,
                    ),
                )
                return _sandbox_response(controller_store, sandbox)
            if detection.get("confirmed_engine") == NO_DATABASE:
                refreshed = read_manifest(controller_store, sandbox_id) or manifest
                write_manifest(
                    controller_store,
                    replace(
                        refreshed,
                        lifecycle_status="ready",
                        operation="resume",
                        operation_phase="ready",
                        db_engine=NO_DATABASE,
                        db_name=None,
                        db_data_volume=None,
                        current_base_commit=(
                            refreshed.pending_base_commit or refreshed.current_base_commit
                        ),
                        pending_base_commit=None,
                        last_error=None,
                    ),
                )
            else:
                database_row = controller_store.sandbox_database(sandbox_id)
                if database_row is not None and database_row.get("status") == "ready":
                    sandbox_database_runtime(docker_client, controller_store, sandbox_id)
                    refreshed = read_manifest(controller_store, sandbox_id) or manifest
                    write_manifest(
                        controller_store,
                        replace(
                            refreshed,
                            lifecycle_status="ready",
                            operation="resume",
                            operation_phase="ready",
                            last_error=None,
                        ),
                    )
                else:
                    _complete_database_provision(
                        docker_client,
                        controller_store,
                        sandbox_id=sandbox_id,
                        operation="resume",
                        rebuild=False,
                    )
            return _sandbox_response(controller_store, sandbox)
    except (SandboxAdmissionError, ValueError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, lifecycle_conflict_detail(error) if isinstance(error, SandboxAdmissionError) else str(error)) from error
    except RuntimeError as error:
        manifest = read_manifest(controller_store, sandbox_id)
        if manifest is not None:
            write_manifest(controller_store, replace(manifest, lifecycle_status="degraded", operation="resume", operation_phase="unsafe_inconsistency", last_error=str(error)))
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except SandboxDatabaseError as error:
        raise HTTPException(error.status_code, error.detail) from error


@router.delete("/{sandbox_id}", response_model=DestroySandboxResponse)
def delete_sandbox(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> DestroySandboxResponse:
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is None:
        tombstone = controller_store.sandbox_tombstone(sandbox_id)
        if tombstone is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
        return _tombstone_response(tombstone)
    try:
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "destroy",
            docker_client=docker_client,
            allow_writers=True,
        ):
            drain_sandbox_writers(docker_client, controller_store, sandbox_id)
            manifest = read_manifest(controller_store, sandbox_id)
            if manifest is None:
                raise RuntimeError("v1 sandbox manifest disappeared during destroy")
            write_manifest(
                controller_store,
                replace(manifest, lifecycle_status="destroying", operation="destroy", operation_phase="sweep", last_error=None),
            )
            drop_sandbox_database(
                docker_client,
                controller_store,
                get_preview_settings(),
                sandbox_id=sandbox_id,
            )
            _sweep_manifest_resources(docker_client, controller_store, sandbox)
            # The tombstone is intentionally after the complete sweep. A
            # failed removal leaves the sandbox visible in destroying.
            tombstone = controller_store.write_sandbox_tombstone(
                sandbox_id,
                reason="destroyed",
                manifest={**sandbox, "resources": controller_store.sandbox_resources(sandbox_id)},
            )
            controller_store.delete_v1_sandbox_manifest(sandbox_id)
    except SandboxAdmissionError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            lifecycle_conflict_detail(error),
        ) from error
    except (DockerException, RuntimeError, ValueError, SandboxDatabaseError) as error:
        manifest = read_manifest(controller_store, sandbox_id)
        if manifest is not None:
            write_manifest(
                controller_store,
                replace(manifest, lifecycle_status="destroying", operation="destroy", operation_phase="sweep", last_error=str(error)),
            )
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error
    return _tombstone_response(tombstone)


def _sweep_manifest_resources(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    sandbox: dict[str, object],
) -> None:
    """Remove only exact manifest entries, after ownership validation."""
    sandbox_id = str(sandbox["id"])
    entries = controller_store.sandbox_resources(sandbox_id)
    workspace = str(sandbox["volume_name"])
    if not any(entry["kind"] == "volume" and entry["name"] == workspace for entry in entries):
        entries.append({"kind": "volume", "name": workspace})
    for entry in entries:
        collection = getattr(docker_client, f"{entry['kind']}s")
        try:
            resource = collection.get(entry["name"])
        except NotFound:
            continue
        validate_ownership(resource, sandbox_id=sandbox_id)
        _remove_manifest_resource(resource, entry["kind"])


def _remove_manifest_resource(resource: object, kind: str) -> None:
    if kind == "network":
        try:
            resource.reload()  # type: ignore[attr-defined]
            endpoint_ids = list((resource.attrs.get("Containers") or {}).keys())  # type: ignore[attr-defined]
        except DockerException:
            endpoint_ids = []
        for endpoint_id in endpoint_ids:
            try:
                resource.disconnect(endpoint_id, force=True)  # type: ignore[attr-defined]
            except DockerException:
                continue
        resource.remove()  # type: ignore[attr-defined]
        return
    resource.remove(force=True)  # type: ignore[attr-defined]


def _tombstone_response(tombstone: dict[str, object]) -> DestroySandboxResponse:
    return DestroySandboxResponse(
        sandbox_id=str(tombstone["sandbox_id"]),
        destroyed_at=str(tombstone["destroyed_at"]),
        reason=str(tombstone["reason"]),
    )


def _sandbox_response(
    controller_store: ControllerStore,
    sandbox: dict[str, object],
) -> SandboxResponse:
    sandbox_id = str(sandbox["id"])
    project = controller_store.project(str(sandbox["project_id"]))
    manifest = read_manifest(controller_store, sandbox_id)
    return SandboxResponse(
        sandbox_id=sandbox_id,
        project_id=str(sandbox["project_id"]),
        lifecycle_version=_value(manifest, "lifecycle_version"),
        feature_key=_value(manifest, "feature_key"),
        feature_title=_value(manifest, "feature_title"),
        desired_state=_value(manifest, "desired_state"),
        lifecycle_status=_value(manifest, "lifecycle_status"),
        base_ref=_value(manifest, "base_ref"),
        feature_branch=_value(manifest, "feature_branch"),
        created_base_commit=_value(manifest, "created_base_commit"),
        current_base_commit=_value(manifest, "current_base_commit"),
        pending_base_commit=_value(manifest, "pending_base_commit"),
        db_engine=_value(manifest, "db_engine"),
        db_name=_value(manifest, "db_name"),
        db_data_volume=_value(manifest, "db_data_volume"),
        schema_baseline_hash=_value(manifest, "schema_baseline_hash"),
        remote_url=str(project["remote_url"]) if project and project.get("remote_url") else None,
    )


def _value(manifest: SandboxManifest, field: str) -> str | None:
    value = getattr(manifest, field)
    return str(value) if value is not None else None


def _require_v1_engine_lifecycle(sandbox: dict[str, object] | None, sandbox_id: str) -> None:
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Legacy sandbox '{sandbox_id}' does not support engine confirmation",
        )


def _require_v1_staleness(
    sandbox: dict[str, object] | None, sandbox_id: str
) -> None:
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Legacy sandbox '{sandbox_id}' has no canonical mirror or usable base commit; recreate it explicitly to use v1 staleness.",
        )


def _require_v1_sync(
    sandbox: dict[str, object] | None, sandbox_id: str
) -> None:
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Legacy sandbox '{sandbox_id}' has no canonical mirror or usable base commit; recreate it explicitly to use v1 sync.",
        )


def _require_v1_publish(
    sandbox: dict[str, object] | None, sandbox_id: str
) -> None:
    if sandbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Legacy sandbox '{sandbox_id}' cannot publish to a remote; recreate it explicitly as v1.",
        )


def _required_sync_value(
    sandbox: dict[str, object], field: str, sandbox_id: str
) -> str:
    value = sandbox.get(field)
    if value is None or not str(value):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no {field}; recreate it explicitly to use v1 sync.",
        )
    return str(value)


def _sync_strategy(store: ControllerStore, sandbox_id: str) -> str:
    """Merge only when the publication table observes an open PR."""
    publication = store.sandbox_publication(sandbox_id)
    if (
        publication is not None
        and publication.get("pr_number") is not None
        and str(publication.get("pr_state") or "").lower() == "open"
    ):
        return "merge"
    return "rebase"


def _base_branch(base_ref: str) -> str:
    prefix = "refs/heads/"
    if not base_ref.startswith(prefix) or not base_ref[len(prefix) :]:
        raise PublishError(409, "Sandbox has an invalid base branch for pull request publishing")
    return base_ref[len(prefix) :]


def _sync_engine_report(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    image: str,
) -> EngineSyncReport:
    """Report the new-tree detection result without changing confirmed intent."""
    stored = controller_store.sandbox_engine_detection(sandbox_id) or {}
    confirmed = _optional_string(stored.get("confirmed_engine"))
    try:
        detected = discover_engine(
            docker_client,
            image=image,
            volume_name=workspace_volume(sandbox_id),
        )
    except Exception as error:
        return EngineSyncReport(
            confirmed_engine=confirmed,
            detected_engine=None,
            mismatch=False,
            detection_error=str(error),
        )
    detected_engine = detected.proposed_engine
    return EngineSyncReport(
        confirmed_engine=confirmed,
        detected_engine=detected_engine,
        mismatch=bool(
            confirmed and detected_engine and detected_engine != confirmed
        ),
    )


def _required_staleness_value(
    sandbox: dict[str, object], field: str, sandbox_id: str
) -> str:
    value = sandbox.get(field)
    if value is None or not str(value):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Sandbox '{sandbox_id}' has no {field}; recreate it explicitly to use v1 staleness.",
        )
    return str(value)


def _engine_detection_response(detection: dict[str, object]) -> EngineDetectionResponse:
    return EngineDetectionResponse(
        sandbox_id=str(detection["sandbox_id"]),
        signals=_json_list(detection["signals_json"]),
        proposed_engine=_optional_string(detection.get("proposed_engine")),
        confirmed_engine=_optional_string(detection.get("confirmed_engine")),
        migrate_commands=[str(value) for value in _json_value(detection["migrate_commands_json"], [])],
        seed_commands=[str(value) for value in _json_value(detection["seed_commands_json"], [])],
        commands_source={str(key): str(value) for key, value in _json_value(detection["commands_source"], {}).items()},
        detected_at_commit=str(detection["detected_at_commit"]),
        actor=_optional_string(detection.get("actor")),
        confirmed_at=_optional_string(detection.get("confirmed_at")),
    )


def _confirm_engine_snapshot(
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    request: EngineConfirmationRequest,
    detection: dict[str, object],
) -> None:
    proposed_migrate = [str(value) for value in _json_value(detection["migrate_commands_json"], [])]
    proposed_seed = [str(value) for value in _json_value(detection["seed_commands_json"], [])]
    migrate = request.migrate_commands or proposed_migrate
    seed = request.seed_commands or proposed_seed
    if request.engine != NO_DATABASE and not migrate and not seed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Engine confirmation requires project migration or seed commands when detection proposes none",
        )
    sources = request.commands_source or {
        str(key): str(value)
        for key, value in _json_value(detection["commands_source"], {}).items()
    }
    required_sources = ({"migrate"} if migrate else set()) | ({"seed"} if seed else set())
    if required_sources.difference(sources):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "commands_source must identify the source for every approved command set",
        )
    controller_store.confirm_sandbox_engine_detection(
        sandbox_id=sandbox_id,
        engine=request.engine,
        migrate_commands=migrate,
        seed_commands=seed,
        commands_source=sources,
        actor=request.actor,
    )


def _complete_database_provision(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    operation: str,
    rebuild: bool,
) -> None:
    """Provision and migrate from stored approval, then make ready truthful."""
    manifest = read_manifest(controller_store, sandbox_id)
    detection = controller_store.sandbox_engine_detection(sandbox_id)
    if manifest is None or detection is None:
        raise SandboxDatabaseError(409, "Sandbox database intent is incomplete")
    engine = str(detection.get("confirmed_engine") or "")
    if engine == NO_DATABASE:
        write_manifest(
            controller_store,
            replace(
                manifest,
                lifecycle_status="ready",
                operation=operation,
                operation_phase="ready",
                db_engine=engine,
                db_name=None,
                db_data_volume=None,
                current_base_commit=manifest.pending_base_commit or manifest.current_base_commit,
                pending_base_commit=None,
                last_error=None,
            ),
        )
        return
    migrate = [
        str(value)
        for value in _json_value(detection.get("migrate_commands_json"), [])
    ]
    seed = [
        str(value)
        for value in _json_value(detection.get("seed_commands_json"), [])
    ]
    data_volume = db_data_volume(sandbox_id) if engine == "sqlite" else None
    write_manifest(
        controller_store,
        replace(
            manifest,
            lifecycle_status=(
                "syncing"
                if operation == "sync"
                else (
                    "database_failed"
                    if operation == "reset-db"
                    and manifest.lifecycle_status == "database_failed"
                    else "creating"
                )
            ),
            operation=operation,
            operation_phase=("migration_replay" if operation == "sync" else "database_provisioning"),
            db_engine=engine,
            db_name=database_name(sandbox_id),
            db_data_volume=data_volume,
            last_error=None,
        ),
    )
    try:
        settings = get_preview_settings()
        schema_files = discover_schema_baseline_files(
            docker_client,
            image=settings.git_image,
            volume_name=workspace_volume(sandbox_id),
        )
        _runtime, baseline_hash = provision_sandbox_database(
            docker_client,
            controller_store,
            settings,
            sandbox_id=sandbox_id,
            migrate_commands=migrate,
            seed_commands=seed,
            schema_files=schema_files,
            rebuild=rebuild,
        )
    except SandboxMigrationError as error:
        detail = error.detail
        if operation == "sync":
            detail = (
                f"{detail}. Git is updated, but applied migrations or seed commands "
                "are not rolled back. Run reset-db to rebuild the database and "
                "finalize the pending base commit."
            )
        failed = read_manifest(controller_store, sandbox_id)
        if failed is not None:
            write_manifest(
                controller_store,
                replace(
                    failed,
                    lifecycle_status="database_failed",
                    operation=operation,
                    operation_phase="migration_failed",
                    last_error=detail,
                ),
            )
        if operation == "sync":
            raise SandboxMigrationError(error.status_code, detail) from error
        raise
    except Exception as error:
        failed = read_manifest(controller_store, sandbox_id)
        if failed is not None:
            database_failed = operation in {"reset-db", "sync"}
            write_manifest(
                controller_store,
                replace(
                    failed,
                    lifecycle_status="database_failed" if database_failed else "creating",
                    operation=operation,
                    operation_phase="database_provisioning_failed",
                    last_error=str(error),
                ),
            )
        if isinstance(error, SandboxDatabaseError):
            raise
        raise SandboxDatabaseError(503, f"Sandbox database provisioning failed: {error}") from error
    ready = read_manifest(controller_store, sandbox_id)
    if ready is None:
        raise RuntimeError("sandbox manifest disappeared after database provisioning")
    current_base = ready.pending_base_commit or ready.current_base_commit
    write_manifest(
        controller_store,
        replace(
            ready,
            lifecycle_status="ready",
            operation=operation,
            operation_phase="ready",
            current_base_commit=current_base,
            pending_base_commit=None,
            schema_baseline_hash=baseline_hash,
            last_error=None,
        ),
    )


def _json_value(value: object, default: object) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _json_list(value: object) -> list[dict[str, object]]:
    parsed = _json_value(value, [])
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
