from dataclasses import replace

from docker.client import DockerClient
from docker.errors import DockerException, NotFound

from app.containers.config import get_git_settings, get_preview_runtime_limits
from app.controller.store import ControllerStore, SandboxAdmissionError
from app.controller.store.lifecycle_status import SandboxLifecycleStatus
from app.platform.naming import (
    feature_branch,
    mirror_volume,
    sandbox_id_for,
    validate_ownership,
    workspace_volume,
)
from app.platform.remote import project_id_for_remote
from app.sandboxes.database import (
    SandboxDatabaseError,
    drop_sandbox_database,
    sandbox_database_runtime,
)
from app.sandboxes.engine_detection import NO_DATABASE, discover_engine
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
    MirrorPin,
    WorkspaceMissing,
    ensure_project_mirror,
    ensure_workspace_import,
    validate_project_mirror,
    validate_workspace_import,
    verify_workspace_identity,
)

from .engine import _confirm_engine_snapshot
from .errors import SandboxConflict, SandboxInternalFailure, SandboxNotFound
from .outcomes import CreateOutcome, EngineConfirmation
from .provisioning import complete_database_provision
from .resources import _docker_collection, _remove_manifest_resource


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
                git_image = get_git_settings().git_image
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
        with lifecycle_lease(
            controller_store, sandbox_id, "resume", docker_client=docker_client
        ):
            manifest = read_manifest(controller_store, sandbox_id)
            project = controller_store.project(str(sandbox["project_id"]))
            if manifest is None or project is None:
                raise RuntimeError("sandbox manifest or project is missing")
            # The mirror is shared. Validate it under the project lock, but do
            # not retain that lock while inspecting or repairing the workspace.
            try:
                with project_mirror_lock(
                    controller_store, str(project["id"]), "resume"
                ):
                    validate_project_mirror(
                        docker_client, project_id=str(project["id"])
                    )
            except ValueError as error:
                raise RuntimeError(
                    f"unsafe mirror ownership inconsistency: {error}"
                ) from error
            try:
                validate_workspace_import(docker_client, sandbox_id=sandbox_id)
                if not manifest.feature_branch:
                    raise RuntimeError(
                        "workspace feature branch is missing from the manifest"
                    )
                verify_workspace_identity(
                    docker_client,
                    image=get_git_settings().git_image,
                    sandbox_id=sandbox_id,
                    feature_branch=manifest.feature_branch,
                )
            except ValueError as error:
                raise RuntimeError(
                    f"unsafe workspace ownership inconsistency: {error}"
                ) from error
            except WorkspaceMissing:
                # A missing workspace is safe to recreate.  It has no worktree
                # to preserve.  We use the immutable original base, never the
                # latest mirror head.
                if not manifest.created_base_commit or not manifest.feature_branch:
                    raise RuntimeError(
                        "workspace is missing and the immutable clone identity is absent"
                    )
                controller_store.record_sandbox_resource(
                    sandbox_id, kind="volume", name=workspace_volume(sandbox_id)
                )
                ensure_workspace_import(
                    docker_client,
                    image=get_git_settings().git_image,
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
                        image=get_git_settings().git_image,
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
                    sandbox_database_runtime(
                        docker_client, controller_store, sandbox_id
                    )
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
            lifecycle_conflict_detail(error)
            if isinstance(error, SandboxAdmissionError)
            else str(error)
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
                get_preview_runtime_limits(),
                sandbox_id=sandbox_id,
            )
            _sweep_manifest_resources(docker_client, controller_store, sandbox)
            # The tombstone is intentionally after the complete sweep. A
            # failed removal leaves the sandbox visible in destroying.
            tombstone = controller_store.write_sandbox_tombstone(
                sandbox_id,
                reason="destroyed",
                manifest={
                    **sandbox,
                    "resources": controller_store.sandbox_resources(sandbox_id),
                },
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
    if not any(
        entry["kind"] == "volume" and entry["name"] == workspace for entry in entries
    ):
        entries.append({"kind": "volume", "name": workspace})
    for entry in entries:
        collection = _docker_collection(docker_client, entry["kind"])
        try:
            resource = collection.get(entry["name"])
        except NotFound:
            continue
        validate_ownership(resource, sandbox_id=sandbox_id)
        _remove_manifest_resource(resource, entry["kind"])
