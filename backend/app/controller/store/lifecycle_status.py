from collections.abc import Mapping
from enum import StrEnum


class SandboxLifecycleStatus(StrEnum):
    CREATING = "creating"
    AWAITING_ENGINE_CONFIRMATION = "awaiting_engine_confirmation"
    READY = "ready"
    SYNCING = "syncing"
    PUBLISHING = "publishing"
    DATABASE_FAILED = "database_failed"
    DEGRADED = "degraded"
    DRAINING = "draining"
    DESTROYING = "destroying"


# The whole managed-sandbox lifecycle in one place. Manifest writes derive
# their guarded UPDATE sources from this table rather than accepting a source
# from a workflow caller.
SANDBOX_LIFECYCLE_TRANSITIONS: Mapping[
    SandboxLifecycleStatus, frozenset[SandboxLifecycleStatus]
] = {
    SandboxLifecycleStatus.CREATING: frozenset(
        {
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
            SandboxLifecycleStatus.READY,
            SandboxLifecycleStatus.DATABASE_FAILED,
            SandboxLifecycleStatus.DEGRADED,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION: frozenset(
        {
            SandboxLifecycleStatus.CREATING,
            SandboxLifecycleStatus.DEGRADED,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    SandboxLifecycleStatus.READY: frozenset(
        {
            # Reset enters provisioning before it rebuilds a ready database.
            SandboxLifecycleStatus.CREATING,
            SandboxLifecycleStatus.SYNCING,
            SandboxLifecycleStatus.PUBLISHING,
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
            SandboxLifecycleStatus.DEGRADED,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    # Resume repairs operations interrupted by a process exit. It rechecks the
    # persisted engine decision and either resumes provisioning, asks a person,
    # restores readiness, or records an inconsistency it cannot repair safely.
    SandboxLifecycleStatus.SYNCING: frozenset(
        {
            SandboxLifecycleStatus.CREATING,
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
            SandboxLifecycleStatus.READY,
            SandboxLifecycleStatus.DATABASE_FAILED,
            SandboxLifecycleStatus.DEGRADED,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    SandboxLifecycleStatus.PUBLISHING: frozenset(
        {
            SandboxLifecycleStatus.CREATING,
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
            SandboxLifecycleStatus.READY,
            SandboxLifecycleStatus.DEGRADED,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    SandboxLifecycleStatus.DATABASE_FAILED: frozenset(
        {
            SandboxLifecycleStatus.CREATING,
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
            SandboxLifecycleStatus.READY,
            SandboxLifecycleStatus.DEGRADED,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    SandboxLifecycleStatus.DEGRADED: frozenset(
        {
            SandboxLifecycleStatus.CREATING,
            SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION,
            SandboxLifecycleStatus.READY,
            SandboxLifecycleStatus.DRAINING,
            SandboxLifecycleStatus.DESTROYING,
        }
    ),
    # Destroy accepts every managed status. Draining declares the destructive
    # intent and blocks writers before destroying starts the resource sweep.
    SandboxLifecycleStatus.DRAINING: frozenset(
        {SandboxLifecycleStatus.DESTROYING}
    ),
    # A repeated destroy reasserts draining before it retries the sweep.
    SandboxLifecycleStatus.DESTROYING: frozenset(
        {SandboxLifecycleStatus.DRAINING}
    ),
}


def source_statuses(
    target: SandboxLifecycleStatus,
) -> frozenset[SandboxLifecycleStatus]:
    return frozenset(
        source
        for source, targets in SANDBOX_LIFECYCLE_TRANSITIONS.items()
        if target in targets
    )
