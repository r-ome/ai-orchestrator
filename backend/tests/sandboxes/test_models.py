from app.sandboxes.models import (
    SANDBOX_LIFECYCLE_TRANSITIONS,
    SandboxLifecycleStatus,
    source_statuses,
)


def test_lifecycle_statuses_cover_persisted_values() -> None:
    assert {status.value for status in SandboxLifecycleStatus} == {
        "creating",
        "awaiting_engine_confirmation",
        "ready",
        "syncing",
        "publishing",
        "database_failed",
        "degraded",
        "draining",
        "destroying",
    }


def test_lifecycle_sources_are_the_inverse_of_the_transition_table() -> None:
    for target in SandboxLifecycleStatus:
        assert source_statuses(target) == frozenset(
            source
            for source, targets in SANDBOX_LIFECYCLE_TRANSITIONS.items()
            if target in targets
        )


def test_destroy_and_resume_recovery_edges_are_explicit() -> None:
    assert source_statuses(SandboxLifecycleStatus.DRAINING) == frozenset(
        SandboxLifecycleStatus
    ).difference({SandboxLifecycleStatus.DRAINING})
    assert source_statuses(SandboxLifecycleStatus.DESTROYING) == frozenset(
        SandboxLifecycleStatus
    ).difference({SandboxLifecycleStatus.DESTROYING})
    assert SandboxLifecycleStatus.CREATING in SANDBOX_LIFECYCLE_TRANSITIONS[
        SandboxLifecycleStatus.DEGRADED
    ]
    assert SandboxLifecycleStatus.READY in SANDBOX_LIFECYCLE_TRANSITIONS[
        SandboxLifecycleStatus.DEGRADED
    ]
