"""Sandbox workflows independent of HTTP transport."""

import json
from dataclasses import dataclass, replace

from docker.client import DockerClient
from docker.errors import DockerException

from app.controller.store import ControllerStore, SandboxAdmissionError
from app.previews.config import get_preview_settings
from app.sandboxes.database import (
    SandboxDatabaseError,
    SandboxMigrationError,
    provision_sandbox_database,
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
    lifecycle_conflict_detail,
    lifecycle_lease,
    project_mirror_lock,
)
from app.sandboxes.manifest import (
    read_manifest,
    transition_sandbox_lifecycle,
    write_manifest,
)
from app.sandboxes.models import SandboxLifecycleStatus
from app.sandboxes.naming import database_name, db_data_volume, workspace_volume
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
