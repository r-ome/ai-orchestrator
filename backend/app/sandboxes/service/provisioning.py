from dataclasses import replace

from docker.client import DockerClient

from app.controller.store import ControllerStore, SandboxAdmissionError
from app.controller.store.lifecycle_status import SandboxLifecycleStatus
from app.platform.naming import database_name, db_data_volume, workspace_volume
from app.previews.config import get_preview_settings
from app.sandboxes.database import (
    SandboxDatabaseError,
    SandboxMigrationError,
    provision_sandbox_database,
)
from app.sandboxes.engine_detection import NO_DATABASE, discover_schema_baseline_files
from app.sandboxes.lifecycle import lifecycle_conflict_detail, lifecycle_lease
from app.sandboxes.manifest import (
    read_manifest,
    transition_sandbox_lifecycle,
    write_manifest,
)

from .coercion import _json_value, require_v1
from .errors import SandboxConflict


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
                current_base_commit=manifest.pending_base_commit
                or manifest.current_base_commit,
                pending_base_commit=None,
                last_error=None,
            ),
            to_status=SandboxLifecycleStatus.READY,
        )
        return
    migrate = [
        str(value) for value in _json_value(detection.get("migrate_commands_json"), [])
    ]
    seed = [
        str(value) for value in _json_value(detection.get("seed_commands_json"), [])
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
        raise SandboxDatabaseError(
            503, f"Sandbox database provisioning failed: {error}"
        ) from error
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
