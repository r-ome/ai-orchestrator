"""Sandbox manifest API."""

from typing import Annotated, Callable, TypeVar

from docker.client import DockerClient
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, field_validator

from app.controller.store import ControllerStore, get_controller_store
from app.docker_client import get_docker_client
from app.docker_errors import DockerErrorPolicy, PassThroughApiError, docker_response
from app.projects.remote import normalize_remote_url
from app.sandboxes.manifest import (
    SandboxManifest,
    read_manifest,
)
from app.sandboxes.models import SandboxLifecycleStatus
from app.sandboxes.database import SandboxDatabaseError
from app.sandboxes.engine_detection import normalize_confirmable_engine
from app.sandboxes import service
from app.sandboxes.service import (
    EngineConfirmation,
    SandboxConflict,
    SandboxDependencyFailure,
    SandboxInternalFailure,
    SandboxNotFound,
    SandboxUnavailable,
    SandboxValidationError,
    _json_value,
    _optional_string,
    publish,
    require_v1,
    sync,
)

from app.sandboxes.publish import (
    PublishError,
)
from app.sandboxes.naming import validate_feature_key
from app.sandboxes.orphans import (
    parse_orphan_resource_key,
    resource_is_claimed,
)


router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])
ResponseType = TypeVar("ResponseType")


_DOCKER_ERRORS = DockerErrorPolicy(
    domain_errors=(
        SandboxNotFound,
        SandboxConflict,
        SandboxDependencyFailure,
        SandboxInternalFailure,
        SandboxValidationError,
        SandboxUnavailable,
        SandboxDatabaseError,
        PublishError,
    ),
    api_error=PassThroughApiError("Docker rejected the sandbox operation"),
)


def _docker_response(function: Callable[[], ResponseType]) -> ResponseType:
    return docker_response(function, _DOCKER_ERRORS)


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
    lifecycle_status: SandboxLifecycleStatus | None = None
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
    outcome = _docker_response(
        lambda: service.create_or_resolve(
            docker_client,
            controller_store,
            remote_url=request.remote_url,
            feature_key=request.feature_key,
            feature_title=request.feature_title,
            agent_provider=request.agent_provider,
            stop_blocking_previews=request.stop_blocking_previews,
            engine_confirmation=(
                _engine_confirmation(request.engine_confirmation)
                if request.engine_confirmation is not None
                else None
            ),
        )
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _sandbox_response(controller_store, outcome.sandbox)


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
    orphan_key = _docker_response(
        lambda: service.remove_orphan_resource(
            docker_client,
            controller_store,
            resource=resource,
        )
    )
    return RemoveOrphanResourceResponse(resource=orphan_key, removed=True)


@router.get("/{sandbox_id}", response_model=SandboxResponse)
def get_sandbox(
    sandbox_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    return _docker_response(
        lambda: _sandbox_response(
            controller_store,
            _require_sandbox(controller_store, sandbox_id),
        )
    )


@router.get("/{sandbox_id}/staleness", response_model=SandboxStalenessResponse)
def get_sandbox_staleness(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxStalenessResponse:
    """Fetch the shared mirror, then report this sandbox's informational lag."""
    outcome = _docker_response(
        lambda: service.staleness(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
        )
    )
    return SandboxStalenessResponse(**outcome.__dict__)


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
    outcome = _docker_response(
        lambda: sync(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
            stop_blocking_preview=request.stop_blocking_preview,
        )
    )
    response = _sandbox_response(controller_store, outcome.sandbox)
    return SyncSandboxResponse(
        **response.model_dump(),
        operation_id=outcome.operation_id,
        safety_ref=outcome.safety_ref,
        strategy=outcome.strategy,
        engine_report=EngineSyncReport(**outcome.engine_report.__dict__),
    )


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
    outcome = _docker_response(
        lambda: publish(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
            stop_blocking_preview=request.stop_blocking_preview,
        )
    )
    return PublishSandboxResponse(**outcome.__dict__)


@router.get("/{sandbox_id}/publication", response_model=SandboxPublicationResponse)
def get_sandbox_publication(
    sandbox_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxPublicationResponse:
    return _docker_response(
        lambda: _sandbox_publication_response(controller_store, sandbox_id)
    )


@router.get("/{sandbox_id}/engine", response_model=EngineDetectionResponse)
def get_engine_detection(
    sandbox_id: str,
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> EngineDetectionResponse:
    return _docker_response(
        lambda: _sandbox_engine_detection_response(controller_store, sandbox_id)
    )


@router.post("/{sandbox_id}/confirm-engine", response_model=SandboxResponse)
def confirm_engine(
    sandbox_id: str,
    request: EngineConfirmationRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Freeze a human-approved engine and resume the creation lifecycle."""
    sandbox = _docker_response(
        lambda: service.confirm_engine(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
            confirmation=_engine_confirmation(request),
        )
    )
    return _sandbox_response(controller_store, sandbox)


@router.post("/{sandbox_id}/reset-db", response_model=SandboxResponse)
def reset_database(
    sandbox_id: str,
    request: ResetDatabaseRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Drop and rebuild from the stored, human-approved command snapshot."""
    sandbox = _docker_response(
        lambda: service.reset_database(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
            stop_blocking_preview=request.stop_blocking_preview,
        )
    )
    return _sandbox_response(controller_store, sandbox)


@router.post("/{sandbox_id}/resume", response_model=SandboxResponse)
def resume_sandbox(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> SandboxResponse:
    """Converge safe missing v1 resources without replacing workspace state."""
    sandbox = _docker_response(
        lambda: service.resume(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
        )
    )
    return _sandbox_response(controller_store, sandbox)


@router.delete("/{sandbox_id}", response_model=DestroySandboxResponse)
def delete_sandbox(
    sandbox_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    controller_store: Annotated[ControllerStore, Depends(get_controller_store)],
) -> DestroySandboxResponse:
    tombstone = _docker_response(
        lambda: service.destroy(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
        )
    )
    return _tombstone_response(tombstone)



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


def _require_sandbox(
    controller_store: ControllerStore,
    sandbox_id: str,
) -> dict[str, object]:
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is None:
        raise SandboxNotFound("Sandbox not found")
    return sandbox


def _sandbox_publication_response(
    controller_store: ControllerStore,
    sandbox_id: str,
) -> SandboxPublicationResponse:
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(
        sandbox,
        sandbox_id,
        "cannot publish to a remote; recreate it explicitly as v1.",
    )
    publication = controller_store.sandbox_publication(sandbox_id)
    if publication is None:
        raise SandboxNotFound("Sandbox has no publication record")
    return SandboxPublicationResponse(**publication)


def _sandbox_engine_detection_response(
    controller_store: ControllerStore,
    sandbox_id: str,
) -> EngineDetectionResponse:
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(sandbox, sandbox_id, "does not support engine confirmation")
    detection = controller_store.sandbox_engine_detection(sandbox_id)
    if detection is None:
        raise SandboxNotFound("Engine detection is not available")
    return _engine_detection_response(detection)


def _value(manifest: SandboxManifest, field: str) -> str | None:
    value = getattr(manifest, field)
    return str(value) if value is not None else None



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



def _engine_confirmation(request: EngineConfirmationRequest) -> EngineConfirmation:
    return EngineConfirmation(
        engine=request.engine,
        migrate_commands=request.migrate_commands,
        seed_commands=request.seed_commands,
        commands_source=request.commands_source,
        actor=request.actor,
    )


def _json_list(value: object) -> list[dict[str, object]]:
    parsed = _json_value(value, [])
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
