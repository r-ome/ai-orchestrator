from dataclasses import dataclass


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


@dataclass(frozen=True)
class StalenessOutcome:
    behind_count: int | None
    base_ref: str
    current_base_commit: str
    mirror_fetched_at: str | None
    stale_answer: bool
    fetch_failure_reason: str | None
