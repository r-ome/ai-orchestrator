from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.models import AgentProvider


class Complexity(StrEnum):
    """Reasoning difficulty, not changed-line count."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DelegationStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    HALTED = "halted"
    ABANDONED = "abandoned"


class IntegrationReviewStatus(StrEnum):
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class ChangeRequestStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"


DELEGATION_TRANSITIONS: Mapping[DelegationStatus, frozenset[DelegationStatus]] = {
    DelegationStatus.READY: frozenset(
        {DelegationStatus.RUNNING, DelegationStatus.ABANDONED}
    ),
    DelegationStatus.RUNNING: frozenset(
        {
            DelegationStatus.COMPLETED,
            DelegationStatus.HALTED,
            DelegationStatus.ABANDONED,
        }
    ),
    DelegationStatus.HALTED: frozenset(
        {DelegationStatus.RUNNING, DelegationStatus.ABANDONED}
    ),
    DelegationStatus.COMPLETED: frozenset(),
    DelegationStatus.ABANDONED: frozenset(),
}

TERMINAL_DELEGATION_STATUSES = frozenset(
    status for status, exits in DELEGATION_TRANSITIONS.items() if not exits
)
ACTIVE_DELEGATION_STATUSES = (
    frozenset(DELEGATION_TRANSITIONS) - TERMINAL_DELEGATION_STATUSES
)


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class FailureKind(StrEnum):
    PROVIDER = "provider"
    VERIFICATION = "verification"
    IMPLEMENTATION = "implementation"
    DEFINITION = "definition"
    UNKNOWN = "unknown"


def delegation_source_statuses(
    target: DelegationStatus,
) -> frozenset[DelegationStatus]:
    return frozenset(
        status for status, exits in DELEGATION_TRANSITIONS.items() if target in exits
    )


class WorkItemState(StrEnum):
    """A state derived from dependencies and retained attempts."""

    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class VerificationIntent(BaseModel):
    command_kind: str
    reason: str = ""


class WorkItem(BaseModel):
    id: str
    delegation_id: str
    key: str
    position: int
    title: str
    objective: str
    scope: str
    out_of_scope: str = ""
    dependencies: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    write_scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification: list[VerificationIntent] = Field(default_factory=list)
    complexity: Complexity
    architecture: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    created_at: str


class RunUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None


class WorkItemRun(BaseModel):
    id: str
    work_item_id: str
    delegation_id: str
    attempt: int
    status: RunStatus
    provider: AgentProvider | None = None
    model: str | None = None
    routing_source: str | None = None
    task_id: str | None = None
    task_status: str | None = None
    result: dict[str, Any] | None = None
    failure_kind: FailureKind | None = None
    error: str | None = None
    verification: dict[str, Any] | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
    duration_ms: int | None = None
    exit_code: int | None = None
    repair_count: int = 0
    created_at: str
    updated_at: str
    settled_at: str | None = None


class Delegation(BaseModel):
    id: str
    session_id: str
    sandbox_id: str
    context_id: str | None = None
    revision: int
    status: DelegationStatus
    error: str | None = None
    created_at: str
    updated_at: str
    settled_at: str | None = None


class IntegrationFinding(BaseModel):
    severity: str
    text: str
    work_item_keys: list[str] = Field(default_factory=list)


class IntegrationReview(BaseModel):
    id: str
    delegation_id: str
    revision: int
    status: IntegrationReviewStatus
    provider: AgentProvider | None = None
    model: str | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    approved: bool | None = None
    summary: str = ""
    findings: list[IntegrationFinding] = Field(default_factory=list)
    error: str | None = None
    created_at: str
    updated_at: str
    settled_at: str | None = None
    source_merged_at: str | None = None


class FeatureDiffFile(BaseModel):
    path: str
    additions: int | None = None
    deletions: int | None = None
    binary: bool = False


class FeatureDiff(BaseModel):
    review_id: str | None = None
    base_branch: str
    base_commit: str
    head_commit: str
    files: list[FeatureDiffFile] = Field(default_factory=list)
    additions: int
    deletions: int
    patch: str
    truncated: bool = False


# One revision of a feature diff submitted for review after delegated work.
#
# This is what `change` means in this package: a numbered revision under review,
# not an edit to a sandbox. It *owns* a task through `task_id` — the coding-agent
# turn that produces the revision. A Task is a different concept and lives in
# `app/tasks/`. See CONTEXT.md.
#
# Kept as a comment, not a docstring: pydantic copies a model's docstring into the
# OpenAPI schema as its `description`, which would publish a repo-internal note to
# API consumers.
class FeatureChangeRequest(BaseModel):
    id: str
    delegation_id: str
    revision: int
    status: ChangeRequestStatus
    instructions: str
    provider: AgentProvider
    model: str
    task_id: str | None = None
    verification: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    settled_at: str | None = None


class RequestFeatureChange(BaseModel):
    instructions: str = Field(min_length=1, max_length=20_000)
    provider: AgentProvider = AgentProvider.CLAUDE
    model: str | None = Field(default=None, max_length=100)
    credential_profile: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )


class ItemRouting(BaseModel):
    recommended_model: str
    model: str
    source: str
    provider: AgentProvider
    override_provider: AgentProvider | None = None
    override_model: str | None = None
    warning: str | None = None
    #: Every model each provider serves, best first. The model override is
    #: chosen from the entry for the selected provider, because a model name
    #: only means anything to the provider that serves it.
    models_by_provider: dict[str, list[str]] = Field(default_factory=dict)
    #: What each provider would recommend for this item's complexity, so the
    #: form can show the recommendation before the override is saved.
    recommended_by_provider: dict[str, str] = Field(default_factory=dict)


class WorkItemView(BaseModel):
    item: WorkItem
    state: WorkItemState
    wave: int
    blocked_by: list[str] = Field(default_factory=list)
    can_run_in_parallel_with: list[str] = Field(default_factory=list)
    runs: list[WorkItemRun] = Field(default_factory=list)
    routing: ItemRouting | None = None


class DelegationView(BaseModel):
    delegation: Delegation
    items: list[WorkItemView] = Field(default_factory=list)
    waves: list[list[str]] = Field(default_factory=list)
    ready: list[str] = Field(default_factory=list)
    review: IntegrationReview | None = None
    changes: list[FeatureChangeRequest] = Field(default_factory=list)
    review_superseded: bool = False
    feature_approved: bool = False


class DelegationsResponse(BaseModel):
    count: int
    delegations: list[Delegation]


class GenerateDelegationRequest(BaseModel):
    provider: AgentProvider = AgentProvider.CLAUDE
    model: str | None = Field(default=None, max_length=100)


class SetRoutingRequest(BaseModel):
    provider: AgentProvider | None = None
    model: str | None = Field(default=None, max_length=100)
    actor: str = Field(default="human", max_length=100)


class GenerateIntegrationReviewRequest(BaseModel):
    provider: AgentProvider = AgentProvider.CLAUDE
    model: str | None = Field(default=None, max_length=100)


class GenerateIntegrationReviewOutcome(BaseModel):
    review: IntegrationReview
    accepted: bool
    attempts: int
    validation_errors: list[str] = Field(default_factory=list)
    turn_status: str
    turn_error: str | None = None


class StartRunRequest(BaseModel):
    # None means "no preference for this run", which lets `route` fall through
    # to ROUTING_DEFAULT_PROVIDER. Defaulting to a named provider made every
    # request that omitted the field an override, so a deployment configured
    # for Codex still ran Claude. The unattended driver sends no preference at
    # all, so it would have pinned every item.
    provider: AgentProvider | None = None
    model: str | None = Field(default=None, max_length=100)
    credential_profile: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )


class StartRunOutcome(BaseModel):
    delegation: DelegationView
    run_id: str
    run_status: RunStatus
    task_id: str | None = None
    task_status: str | None = None
    turn_status: str | None = None
    committed: bool | None = None
    result_errors: list[str] = Field(default_factory=list)
    packet: Any | None = None
    model: str = ""
    routing_source: str | None = None
    recommended_model: str | None = None
    routing_warning: str | None = None


class AcceptedJob(BaseModel):
    """A claimed turn the caller follows instead of waiting for.

    `job_id` is whatever the phase reports progress on: a work item run's id, a
    review's id, or — for decomposition, which has no row until it succeeds — a
    generated id that only `delegation.progress` events carry.
    """

    job_id: str
    kind: str
    detail: str


class GenerateDelegationOutcome(BaseModel):
    delegation: DelegationView | None = None
    accepted: bool
    attempts: int
    validation_errors: list[str] = Field(default_factory=list)
    turn_status: str
    turn_error: str | None = None
    model: str | None = None
