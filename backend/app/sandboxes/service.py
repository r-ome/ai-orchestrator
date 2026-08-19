"""Sandbox workflows independent of HTTP transport."""

import json
from dataclasses import dataclass, replace

from docker.client import DockerClient
from docker.errors import DockerException, NotFound

from app.controller.store import ControllerStore, SandboxAdmissionError
from app.previews.config import get_preview_settings
from app.projects.remote import project_id_for_remote
from app.sandboxes.database import (
    SandboxDatabaseError,
    SandboxMigrationError,
    drop_sandbox_database,
    provision_sandbox_database,
    sandbox_database_runtime,
)
from app.sandboxes.engine_detection import NO_DATABASE, discover_engine, discover_schema_baseline_files
from app.sandboxes.git import (
    create_workspace_safety_ref,
    describe_git_failure,
    fetch_canonical_mirror,
    mirror_base_commit,
    require_clean_workspace,
    restore_workspace_safety_ref,
    sync_workspace_from_mirror,
)
from app.sandboxes.lifecycle import (
    drain_sandbox_writers,
    lifecycle_conflict_detail,
    lifecycle_lease,
    project_mirror_lock,
)
from app.sandboxes.manifest import (
    SandboxManifest,
    read_manifest,
    transition_sandbox_lifecycle,
    write_manifest,
)
from app.sandboxes.mirror import (
    WorkspaceMissing,
    ensure_project_mirror,
    ensure_workspace_import,
    validate_project_mirror,
    validate_workspace_import,
    verify_workspace_identity,
)
from app.sandboxes.models import SandboxLifecycleStatus
from app.sandboxes.naming import (
    database_name,
    db_data_volume,
    feature_branch,
    mirror_volume,
    sandbox_id_for,
    validate_ownership,
    workspace_volume,
)
from app.sandboxes.publish import (
    GitHubApiError,
    PublishError,
    discover_or_create_pull_request,
    publish_reviewed_feature,
    reviewed_target,
)


class SandboxConflict(Exception):
    """A sandbox state conflict that maps to HTTP 409."""

    def __init__(self, detail: object) -> None:
        self.detail = detail
        super().__init__(str(detail))


class SandboxDependencyFailure(Exception):
    """A dependency failure that maps to HTTP 424."""


class SandboxNotFound(Exception):
    """A missing sandbox that maps to HTTP 404."""


class SandboxInternalFailure(Exception):
    """An internal sandbox failure that maps to HTTP 500."""


class SandboxValidationError(Exception):
    """A rejected sandbox request that maps to HTTP 422."""


@dataclass(frozen=True)
class EngineConfirmation:
    """One human-approved engine choice, already validated by the caller."""

    engine: str
    migrate_commands: list[str]
    seed_commands: list[str]
    commands_source: dict[str, str]
    actor: str


@dataclass(frozen=True)
class CreateOutcome:
    sandbox: dict[str, object]
    created: bool


@dataclass(frozen=True)
class EngineSyncReport:
    confirmed_engine: str | None
    detected_engine: str | None
    mismatch: bool
    detection_error: str | None = None


@dataclass(frozen=True)
class SyncOutcome:
    sandbox: dict[str, object]
    operation_id: str
    safety_ref: str
    strategy: str
    engine_report: EngineSyncReport


@dataclass(frozen=True)
class PublishOutcome:
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


def sync(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    stop_blocking_preview: bool,
) -> SyncOutcome:
    """Explicitly bring one clean v1 workspace forward from its local mirror."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(sandbox, sandbox_id, "has no canonical mirror or usable base commit; recreate it explicitly to use v1 sync.")
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status is not SandboxLifecycleStatus.READY:
        raise SandboxConflict("Sandbox can sync only from ready")
    project = controller_store.project(str(sandbox["project_id"]))
    if project is None or not project.get("mirror_volume"):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no project mirror; recreate it explicitly to use v1."
        )
    base_ref = _required_sync_value(sandbox, "base_ref", sandbox_id)
    current_base_commit = _required_sync_value(
        sandbox, "current_base_commit", sandbox_id
    )
    if not manifest.feature_branch:
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no feature branch; recreate it explicitly to use v1."
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
            stop_blocking_previews=stop_blocking_preview,
        ) as lease:
            if lease is None:  # _require_v1 above keeps this defensive.
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
                raise SandboxConflict(
                    f"Sandbox workspace is dirty; sync refused before changes: {error}"
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
                pending_base_commit=pending_base_commit,
                last_error=None,
            )
            transition_sandbox_lifecycle(
                controller_store,
                syncing,
                to_status=SandboxLifecycleStatus.SYNCING,
            )
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
                    transition_sandbox_lifecycle(
                        controller_store,
                        replace(
                            failed,
                            current_base_commit=current_base_commit,
                            pending_base_commit=None,
                            last_error=detail,
                        ),
                        to_status=SandboxLifecycleStatus.READY,
                    )
                raise SandboxConflict(detail) from sync_error

            # This runner reads only the approved controller snapshot. It does
            # not read preview configuration or infer commands from the new tree.
            complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="sync",
                rebuild=True,
            )
            engine_report = sync_engine_report(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                image=git_image,
            )
            refreshed = controller_store.sandbox(sandbox_id)
            if refreshed is None:
                raise RuntimeError("sandbox disappeared after sync")
            return SyncOutcome(
                sandbox=refreshed,
                operation_id=operation_id,
                safety_ref=safety_ref,
                strategy=sync_strategy,
                engine_report=engine_report,
            )
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    except SandboxDatabaseError:
        raise
    except (DockerException, RuntimeError, ValueError) as error:
        raise SandboxInternalFailure(str(error)) from error


def publish(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    stop_blocking_preview: bool,
) -> PublishOutcome:
    """Push one reviewed branch, then discover or create and verify its PR."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(sandbox, sandbox_id, "cannot publish to a remote; recreate it explicitly as v1.")
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status is not SandboxLifecycleStatus.READY:
        raise SandboxConflict("Sandbox can publish only from ready")
    if not manifest.feature_branch:
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no feature branch; recreate it explicitly to publish."
        )
    project = controller_store.project(str(sandbox["project_id"]))
    if project is None or not project.get("remote_url") or not project.get("mirror_volume"):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no project mirror or remote; recreate it explicitly to publish."
        )
    prior_publication = controller_store.sandbox_publication(sandbox_id) or {}
    remote_branch = _optional_string(prior_publication.get("remote_branch")) or (
        manifest.remote_branch or manifest.feature_branch
    )
    # Which planning session this push carries. Recorded on the publication so
    # the feature status of a *different* session in the same sandbox does not
    # inherit this pull request.
    owner_session_id = controller_store.publication_owner_session(sandbox_id)
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
            stop_blocking_previews=stop_blocking_preview,
        ) as lease:
            if lease is None:
                raise RuntimeError("managed sandbox did not acquire a lifecycle lease")
            operation_id = str(lease["operation_id"])
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    manifest,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.PUBLISHING,
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
                    transition_sandbox_lifecycle(
                        controller_store,
                        replace(
                            failed,
                            last_error=failure_message,
                        ),
                        to_status=SandboxLifecycleStatus.READY,
                    )
                raise
            publication = controller_store.record_sandbox_publication(
                sandbox_id=sandbox_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                last_error=None,
                session_id=owner_session_id,
            )
            pushed = read_manifest(controller_store, sandbox_id)
            if pushed is None:
                raise RuntimeError("sandbox manifest disappeared during publish")
            write_manifest(
                controller_store,
                replace(
                    pushed,
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
                    transition_sandbox_lifecycle(
                        controller_store,
                        replace(
                            failed,
                            last_error=str(error),
                        ),
                        to_status=SandboxLifecycleStatus.READY,
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
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    completed,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.READY,
            )
            return PublishOutcome(
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
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    except PublishError:
        raise
    except (DockerException, RuntimeError, ValueError) as error:
        raise SandboxDependencyFailure(describe_git_failure(error)) from error


def complete_database_provision(
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
        transition_sandbox_lifecycle(
            controller_store,
            replace(
                manifest,
                db_engine=engine,
                db_name=None,
                db_data_volume=None,
                current_base_commit=manifest.pending_base_commit or manifest.current_base_commit,
                pending_base_commit=None,
                last_error=None,
            ),
            to_status=SandboxLifecycleStatus.READY,
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
    if operation == "sync":
        target_status = SandboxLifecycleStatus.SYNCING
    elif (
        operation == "reset-db"
        and manifest.lifecycle_status is SandboxLifecycleStatus.DATABASE_FAILED
    ):
        target_status = SandboxLifecycleStatus.DATABASE_FAILED
    else:
        target_status = SandboxLifecycleStatus.CREATING
    provisioning = replace(
        manifest,
        db_engine=engine,
        db_name=database_name(sandbox_id),
        db_data_volume=data_volume,
        last_error=None,
    )
    if manifest.lifecycle_status is target_status:
        write_manifest(controller_store, provisioning)
    else:
        transition_sandbox_lifecycle(
            controller_store,
            provisioning,
            to_status=target_status,
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
            migration_failed = replace(
                failed,
                last_error=detail,
            )
            if failed.lifecycle_status is SandboxLifecycleStatus.DATABASE_FAILED:
                write_manifest(controller_store, migration_failed)
            else:
                transition_sandbox_lifecycle(
                    controller_store,
                    migration_failed,
                    to_status=SandboxLifecycleStatus.DATABASE_FAILED,
                )
        if operation == "sync":
            raise SandboxMigrationError(error.status_code, detail) from error
        raise
    except Exception as error:
        failed = read_manifest(controller_store, sandbox_id)
        if failed is not None:
            failure_status = (
                SandboxLifecycleStatus.DATABASE_FAILED
                if operation in {"reset-db", "sync"}
                else SandboxLifecycleStatus.CREATING
            )
            provisioning_failed = replace(
                failed,
                last_error=str(error),
            )
            if failed.lifecycle_status is failure_status:
                write_manifest(controller_store, provisioning_failed)
            else:
                transition_sandbox_lifecycle(
                    controller_store,
                    provisioning_failed,
                    to_status=failure_status,
                )
        if isinstance(error, SandboxDatabaseError):
            raise
        raise SandboxDatabaseError(503, f"Sandbox database provisioning failed: {error}") from error
    ready = read_manifest(controller_store, sandbox_id)
    if ready is None:
        raise RuntimeError("sandbox manifest disappeared after database provisioning")
    current_base = ready.pending_base_commit or ready.current_base_commit
    transition_sandbox_lifecycle(
        controller_store,
        replace(
            ready,
            current_base_commit=current_base,
            pending_base_commit=None,
            schema_baseline_hash=baseline_hash,
            last_error=None,
        ),
        to_status=SandboxLifecycleStatus.READY,
    )


def require_v1(
    sandbox: dict[str, object] | None,
    sandbox_id: str,
    refusal: str,
) -> None:
    if sandbox is None:
        raise SandboxNotFound("Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise SandboxConflict(
            f"Legacy sandbox '{sandbox_id}' {refusal}"
        )


def _required_sync_value(
    sandbox: dict[str, object], field: str, sandbox_id: str
) -> str:
    value = sandbox.get(field)
    if value is None or not str(value):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no {field}; recreate it explicitly to use v1 sync."
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


def sync_engine_report(
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


def _json_value(value: object, default: object) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def create_or_resolve(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    remote_url: str,
    feature_key: str,
    feature_title: str | None,
    agent_provider: str | None,
    stop_blocking_previews: bool,
    engine_confirmation: EngineConfirmation | None,
) -> CreateOutcome:
    """Provision the Phase 4 Git resources for a deterministic v1 sandbox."""
    project_id = project_id_for_remote(remote_url)
    sandbox_id = sandbox_id_for(project_id, feature_key)
    project = controller_store.register_v1_project(
        project_id=project_id,
        remote_url=remote_url,
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
            stop_blocking_previews=stop_blocking_previews,
        ):
            if created:
                write_manifest(
                    controller_store,
                    SandboxManifest(
                        sandbox_id=sandbox_id,
                        lifecycle_version="v1",
                        feature_key=feature_key,
                        feature_title=feature_title,
                        desired_state="active",
                        lifecycle_status=SandboxLifecycleStatus.CREATING,
                        feature_branch=feature_branch(feature_key),
                        agent_provider=agent_provider,
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
                    raise SandboxConflict(str(error)) from error
                return CreateOutcome(sandbox, False)

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
                    or feature_branch(feature_key),
                )
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
                if engine_confirmation is not None:
                    _confirm_engine_snapshot(
                        controller_store,
                        sandbox_id=sandbox_id,
                        confirmation=engine_confirmation,
                        detection=detection_row,
                    )
                    complete_database_provision(
                        docker_client,
                        controller_store,
                        sandbox_id=sandbox_id,
                        operation="create",
                        rebuild=False,
                    )
                else:
                    # A human decision can take an arbitrary time. Leaving
                    # this context releases the lease before we return.
                    transition_sandbox_lifecycle(
                        controller_store,
                        manifest,
                        to_status=SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
                    )
            except ValueError as error:
                raise SandboxConflict(str(error)) from error
            except RuntimeError as error:
                raise SandboxInternalFailure(str(error)) from error
            return CreateOutcome(sandbox, True)
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error


def confirm_engine(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    confirmation: EngineConfirmation,
) -> dict[str, object]:
    """Freeze a human-approved engine and resume the creation lifecycle."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(sandbox, sandbox_id, "does not support engine confirmation")
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if (
        manifest is None
        or manifest.lifecycle_status
        is not SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION
    ):
        raise SandboxConflict("Sandbox is not awaiting engine confirmation")
    detection = controller_store.sandbox_engine_detection(sandbox_id)
    if detection is None:
        raise SandboxConflict("Sandbox has no engine detection to confirm")
    try:
        # This is intentionally a fresh lifecycle lease. The create lease was
        # released before the human received the proposal.
        with lifecycle_lease(controller_store, sandbox_id, "confirm-engine", docker_client=docker_client):
            _confirm_engine_snapshot(
                controller_store,
                sandbox_id=sandbox_id,
                confirmation=confirmation,
                detection=detection,
            )
            current = read_manifest(controller_store, sandbox_id)
            if current is None:
                raise RuntimeError("v1 sandbox manifest disappeared during engine confirmation")
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    current,
                    db_engine=confirmation.engine,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.CREATING,
            )
            complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="confirm-engine",
                rebuild=False,
            )
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    return sandbox


def reset_database(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    stop_blocking_preview: bool,
) -> dict[str, object]:
    """Drop and rebuild from the stored, human-approved command snapshot."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(sandbox, sandbox_id, "does not support engine confirmation")
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if manifest is None or manifest.lifecycle_status not in {
        SandboxLifecycleStatus.READY,
        SandboxLifecycleStatus.DATABASE_FAILED,
    }:
        raise SandboxConflict(
            "Sandbox database can reset only from ready or database_failed"
        )
    if manifest.db_engine == NO_DATABASE:
        raise SandboxConflict(f"Sandbox '{sandbox_id}' has no database to reset")
    try:
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "reset-db",
            docker_client=docker_client,
            stop_blocking_previews=stop_blocking_preview,
        ):
            write_manifest(
                controller_store,
                replace(
                    manifest,
                    last_error=None,
                ),
            )
            complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="reset-db",
                rebuild=True,
            )
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    return sandbox


def resume(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
) -> dict[str, object]:
    """Converge safe missing v1 resources without replacing workspace state."""
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is None:
        raise SandboxNotFound("Sandbox not found")
    if sandbox.get("lifecycle_version") != "v1":
        raise SandboxConflict("Legacy sandboxes do not support resume")
    if sandbox.get("desired_state") != "active":
        raise SandboxConflict("Destroyed sandboxes cannot resume")
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
            except WorkspaceMissing:
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
            if (
                manifest.lifecycle_status
                is SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION
            ):
                return sandbox
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
                transition_sandbox_lifecycle(
                    controller_store,
                    replace(
                        refreshed,
                        last_error=None,
                    ),
                    to_status=SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
                )
                return sandbox
            if detection.get("confirmed_engine") == NO_DATABASE:
                refreshed = read_manifest(controller_store, sandbox_id) or manifest
                ready = replace(
                    refreshed,
                    db_engine=NO_DATABASE,
                    db_name=None,
                    db_data_volume=None,
                    current_base_commit=(
                        refreshed.pending_base_commit or refreshed.current_base_commit
                    ),
                    pending_base_commit=None,
                    last_error=None,
                )
                if refreshed.lifecycle_status is SandboxLifecycleStatus.READY:
                    write_manifest(controller_store, ready)
                else:
                    transition_sandbox_lifecycle(
                        controller_store,
                        ready,
                        to_status=SandboxLifecycleStatus.READY,
                    )
            else:
                database_row = controller_store.sandbox_database(sandbox_id)
                if database_row is not None and database_row.get("status") == "ready":
                    sandbox_database_runtime(docker_client, controller_store, sandbox_id)
                    refreshed = read_manifest(controller_store, sandbox_id) or manifest
                    ready = replace(
                        refreshed,
                        last_error=None,
                    )
                    if refreshed.lifecycle_status is SandboxLifecycleStatus.READY:
                        write_manifest(controller_store, ready)
                    else:
                        transition_sandbox_lifecycle(
                            controller_store,
                            ready,
                            to_status=SandboxLifecycleStatus.READY,
                        )
                else:
                    complete_database_provision(
                        docker_client,
                        controller_store,
                        sandbox_id=sandbox_id,
                        operation="resume",
                        rebuild=False,
                    )
            return sandbox
    except (SandboxAdmissionError, ValueError) as error:
        raise SandboxConflict(
            lifecycle_conflict_detail(error) if isinstance(error, SandboxAdmissionError) else str(error)
        ) from error
    except RuntimeError as error:
        manifest = read_manifest(controller_store, sandbox_id)
        if manifest is not None:
            degraded = replace(
                manifest,
                last_error=str(error),
            )
            if manifest.lifecycle_status is SandboxLifecycleStatus.DEGRADED:
                write_manifest(controller_store, degraded)
            else:
                transition_sandbox_lifecycle(
                    controller_store,
                    degraded,
                    to_status=SandboxLifecycleStatus.DEGRADED,
                )
        raise SandboxConflict(str(error)) from error


def destroy(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
) -> dict[str, object]:
    """Drain, sweep and tombstone one sandbox, returning its tombstone row."""
    sandbox = controller_store.sandbox(sandbox_id)
    if sandbox is None:
        tombstone = controller_store.sandbox_tombstone(sandbox_id)
        if tombstone is None:
            raise SandboxNotFound("Sandbox not found")
        return tombstone
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
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    manifest,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.DESTROYING,
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
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    except (DockerException, RuntimeError, ValueError, SandboxDatabaseError) as error:
        manifest = read_manifest(controller_store, sandbox_id)
        if manifest is not None:
            destroying = replace(
                manifest,
                last_error=str(error),
            )
            if manifest.lifecycle_status is SandboxLifecycleStatus.DESTROYING:
                write_manifest(controller_store, destroying)
            else:
                transition_sandbox_lifecycle(
                    controller_store,
                    destroying,
                    to_status=SandboxLifecycleStatus.DESTROYING,
                )
        raise SandboxInternalFailure(str(error)) from error
    return tombstone


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
        collection = _docker_collection(docker_client, entry["kind"])
        try:
            resource = collection.get(entry["name"])
        except NotFound:
            continue
        validate_ownership(resource, sandbox_id=sandbox_id)
        _remove_manifest_resource(resource, entry["kind"])


def _docker_collection(docker_client: DockerClient, kind: str) -> object:
    """Return the Docker collection for a supported resource kind."""
    collections = {
        "volume": docker_client.volumes,
        "container": docker_client.containers,
        "network": docker_client.networks,
    }
    try:
        return collections[kind]
    except KeyError as error:
        raise SandboxValidationError(
            f"Unsupported sandbox resource kind: {kind}"
        ) from error


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


def _confirm_engine_snapshot(
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    confirmation: EngineConfirmation,
    detection: dict[str, object],
) -> None:
    proposed_migrate = [str(value) for value in _json_value(detection["migrate_commands_json"], [])]
    proposed_seed = [str(value) for value in _json_value(detection["seed_commands_json"], [])]
    migrate = confirmation.migrate_commands or proposed_migrate
    seed = confirmation.seed_commands or proposed_seed
    if confirmation.engine != NO_DATABASE and not migrate and not seed:
        raise SandboxValidationError(
            "Engine confirmation requires project migration or seed commands when detection proposes none"
        )
    sources = confirmation.commands_source or {
        str(key): str(value)
        for key, value in _json_value(detection["commands_source"], {}).items()
    }
    required_sources = ({"migrate"} if migrate else set()) | ({"seed"} if seed else set())
    if required_sources.difference(sources):
        raise SandboxValidationError(
            "commands_source must identify the source for every approved command set"
        )
    controller_store.confirm_sandbox_engine_detection(
        sandbox_id=sandbox_id,
        engine=confirmation.engine,
        migrate_commands=migrate,
        seed_commands=seed,
        commands_source=sources,
        actor=confirmation.actor,
    )
