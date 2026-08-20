"""Sandbox workflows independent of HTTP transport."""

import json as json
from dataclasses import (
    dataclass as dataclass,
)
from dataclasses import (
    replace as replace,
)

from docker.client import (
    DockerClient as DockerClient,
)
from docker.errors import (
    DockerException as DockerException,
)
from docker.errors import (
    NotFound as NotFound,
)

from app.controller.store import (
    ControllerStore as ControllerStore,
)
from app.controller.store import (
    SandboxAdmissionError as SandboxAdmissionError,
)
from app.controller.store.lifecycle_status import (
    SandboxLifecycleStatus as SandboxLifecycleStatus,
)
from app.platform.naming import (
    database_name as database_name,
)
from app.platform.naming import (
    db_data_volume as db_data_volume,
)
from app.platform.naming import (
    feature_branch as feature_branch,
)
from app.platform.naming import (
    is_shared_infrastructure as is_shared_infrastructure,
)
from app.platform.naming import (
    mirror_volume as mirror_volume,
)
from app.platform.naming import (
    orphan_ownership_sandbox_id as orphan_ownership_sandbox_id,
)
from app.platform.naming import (
    sandbox_id_for as sandbox_id_for,
)
from app.platform.naming import (
    validate_ownership as validate_ownership,
)
from app.platform.naming import (
    workspace_volume as workspace_volume,
)
from app.platform.remote import (
    project_id_for_remote as project_id_for_remote,
)
from app.previews.config import (
    get_preview_settings as get_preview_settings,
)
from app.sandboxes.database import (
    SandboxDatabaseError as SandboxDatabaseError,
)
from app.sandboxes.database import (
    SandboxMigrationError as SandboxMigrationError,
)
from app.sandboxes.database import (
    drop_sandbox_database as drop_sandbox_database,
)
from app.sandboxes.database import (
    provision_sandbox_database as provision_sandbox_database,
)
from app.sandboxes.database import (
    sandbox_database_runtime as sandbox_database_runtime,
)
from app.sandboxes.engine_detection import (
    NO_DATABASE as NO_DATABASE,
)
from app.sandboxes.engine_detection import (
    discover_engine as discover_engine,
)
from app.sandboxes.engine_detection import (
    discover_schema_baseline_files as discover_schema_baseline_files,
)
from app.sandboxes.git import (
    count_mirror_staleness as count_mirror_staleness,
)
from app.sandboxes.git import (
    create_workspace_safety_ref as create_workspace_safety_ref,
)
from app.sandboxes.git import (
    describe_git_failure as describe_git_failure,
)
from app.sandboxes.git import (
    fetch_canonical_mirror as fetch_canonical_mirror,
)
from app.sandboxes.git import (
    mirror_base_commit as mirror_base_commit,
)
from app.sandboxes.git import (
    require_clean_workspace as require_clean_workspace,
)
from app.sandboxes.git import (
    restore_workspace_safety_ref as restore_workspace_safety_ref,
)
from app.sandboxes.git import (
    sync_workspace_from_mirror as sync_workspace_from_mirror,
)
from app.sandboxes.lifecycle import (
    drain_sandbox_writers as drain_sandbox_writers,
)
from app.sandboxes.lifecycle import (
    lifecycle_conflict_detail as lifecycle_conflict_detail,
)
from app.sandboxes.lifecycle import (
    lifecycle_lease as lifecycle_lease,
)
from app.sandboxes.lifecycle import (
    project_mirror_lock as project_mirror_lock,
)
from app.sandboxes.manifest import (
    SandboxManifest as SandboxManifest,
)
from app.sandboxes.manifest import (
    read_manifest as read_manifest,
)
from app.sandboxes.manifest import (
    transition_sandbox_lifecycle as transition_sandbox_lifecycle,
)
from app.sandboxes.manifest import (
    write_manifest as write_manifest,
)
from app.sandboxes.mirror import (
    WorkspaceMissing as WorkspaceMissing,
)
from app.sandboxes.mirror import (
    ensure_project_mirror as ensure_project_mirror,
)
from app.sandboxes.mirror import (
    ensure_workspace_import as ensure_workspace_import,
)
from app.sandboxes.mirror import (
    validate_project_mirror as validate_project_mirror,
)
from app.sandboxes.mirror import (
    validate_workspace_import as validate_workspace_import,
)
from app.sandboxes.mirror import (
    verify_workspace_identity as verify_workspace_identity,
)
from app.sandboxes.orphans import (
    parse_orphan_resource_key as parse_orphan_resource_key,
)
from app.sandboxes.orphans import (
    resource_is_claimed as resource_is_claimed,
)
from app.sandboxes.publish import (
    GitHubApiError as GitHubApiError,
)
from app.sandboxes.publish import (
    PublishError as PublishError,
)
from app.sandboxes.publish import (
    discover_or_create_pull_request as discover_or_create_pull_request,
)
from app.sandboxes.publish import (
    publish_reviewed_feature as publish_reviewed_feature,
)
from app.sandboxes.publish import (
    reviewed_target as reviewed_target,
)

from .coercion import (
    _base_branch as _base_branch,
)
from .coercion import (
    _json_value as _json_value,
)
from .coercion import (
    _optional_string as _optional_string,
)
from .coercion import (
    _required_staleness_value as _required_staleness_value,
)
from .coercion import (
    _required_sync_value as _required_sync_value,
)
from .coercion import (
    _sync_strategy as _sync_strategy,
)
from .coercion import (
    require_v1 as require_v1,
)
from .engine import (
    _confirm_engine_snapshot as _confirm_engine_snapshot,
)
from .engine import (
    confirm_engine as confirm_engine,
)
from .errors import (
    SandboxConflict as SandboxConflict,
)
from .errors import (
    SandboxDependencyFailure as SandboxDependencyFailure,
)
from .errors import (
    SandboxInternalFailure as SandboxInternalFailure,
)
from .errors import (
    SandboxNotFound as SandboxNotFound,
)
from .errors import (
    SandboxUnavailable as SandboxUnavailable,
)
from .errors import (
    SandboxValidationError as SandboxValidationError,
)
from .mirror_staleness import (
    staleness as staleness,
)
from .outcomes import (
    CreateOutcome as CreateOutcome,
)
from .outcomes import (
    EngineConfirmation as EngineConfirmation,
)
from .outcomes import (
    EngineSyncReport as EngineSyncReport,
)
from .outcomes import (
    PublishOutcome as PublishOutcome,
)
from .outcomes import (
    StalenessOutcome as StalenessOutcome,
)
from .outcomes import (
    SyncOutcome as SyncOutcome,
)
from .provisioning import (
    complete_database_provision as complete_database_provision,
)
from .provisioning import (
    reset_database as reset_database,
)
from .publishing import (
    publish as publish,
)
from .resources import (
    _docker_collection as _docker_collection,
)
from .resources import (
    _remove_manifest_resource as _remove_manifest_resource,
)
from .resources import (
    remove_orphan_resource as remove_orphan_resource,
)
from .syncing import (
    sync as sync,
)
from .syncing import (
    sync_engine_report as sync_engine_report,
)
from .transitions import (
    _sweep_manifest_resources as _sweep_manifest_resources,
)
from .transitions import (
    create_or_resolve as create_or_resolve,
)
from .transitions import (
    destroy as destroy,
)
from .transitions import (
    resume as resume,
)

globals().pop("coercion", None)
globals().pop("engine", None)
globals().pop("errors", None)
globals().pop("mirror_staleness", None)
globals().pop("outcomes", None)
globals().pop("provisioning", None)
globals().pop("publishing", None)
globals().pop("resources", None)
globals().pop("syncing", None)
globals().pop("transitions", None)
