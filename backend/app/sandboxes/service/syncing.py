from dataclasses import replace

from docker.client import DockerClient
from docker.errors import DockerException

from app.containers.config import get_git_settings
from app.containers.git import (
    create_workspace_safety_ref,
    fetch_canonical_mirror,
    mirror_base_commit,
    require_clean_workspace,
    restore_workspace_safety_ref,
    sync_workspace_from_mirror,
)
from app.controller.store import ControllerStore, SandboxAdmissionError
from app.controller.store.lifecycle_status import SandboxLifecycleStatus
from app.platform.naming import workspace_volume
from app.sandboxes.database import SandboxDatabaseError
from app.sandboxes.engine_detection import discover_engine
from app.sandboxes.lifecycle import (
    lifecycle_conflict_detail,
    lifecycle_lease,
    project_mirror_lock,
)
from app.sandboxes.manifest import read_manifest, transition_sandbox_lifecycle

from .coercion import _optional_string, _required_sync_value, _sync_strategy, require_v1
from .errors import SandboxConflict, SandboxInternalFailure
from .outcomes import EngineSyncReport, SyncOutcome
from .provisioning import complete_database_provision


def sync(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    stop_blocking_preview: bool,
) -> SyncOutcome:
    """Explicitly bring one clean v1 workspace forward from its local mirror."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(
        sandbox,
        sandbox_id,
        "has no canonical mirror or usable base commit; recreate it explicitly to use v1 sync.",
    )
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if (
        manifest is None
        or manifest.lifecycle_status is not SandboxLifecycleStatus.READY
    ):
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

    git_image = get_git_settings().git_image
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
                except Exception as restore_error:  # noqa: BLE001 - preserve the failed restore detail for the caller
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
    except Exception as error:  # noqa: BLE001 - engine detection failures are returned as report data
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
        mismatch=bool(confirmed and detected_engine and detected_engine != confirmed),
    )
