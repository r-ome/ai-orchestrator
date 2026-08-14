from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agents.models import AgentProvider


class PlanningStatus(StrEnum):
    CLARIFYING = "clarifying"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PLANNING = "planning"
    UNDER_REVIEW = "under_review"
    PLAN_READY = "plan_ready"
    REVIEW_LIMIT_REACHED = "review_limit_reached"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FeatureStatus(StrEnum):
    CLARIFYING = "clarifying"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PLANNING = "planning"
    UNDER_REVIEW = "under_review"
    PLAN_READY = "plan_ready"
    BUILDING = "building"
    BLOCKED = "blocked"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    MERGED = "merged"
    ABANDONED = "abandoned"
    REVIEW_LIMIT_REACHED = "review_limit_reached"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanningTurnState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"


class PlanningRole(StrEnum):
    USER = "user"
    CLARIFIER = "clarifier"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    IMPLEMENTATION_CONTEXT = "implementation_context"
    DELEGATOR = "delegator"
    INTEGRATION_REVIEWER = "integration_reviewer"
    SYSTEM = "system"


class FindingStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    REJECTED = "rejected"
    RESOLVED = "resolved"


TERMINAL_PLANNING_STATUSES: frozenset[PlanningStatus] = frozenset(
    {
        PlanningStatus.PLAN_READY,
        PlanningStatus.REVIEW_LIMIT_REACHED,
        PlanningStatus.FAILED,
        PlanningStatus.CANCELLED,
    }
)

# The whole status machine, in one place. Anything absent here is
# unreachable, because the service derives the guarded UPDATE's source
# statuses from this table and never accepts them from a caller.
PLANNING_TRANSITIONS: Mapping[PlanningStatus, frozenset[PlanningStatus]] = {
    PlanningStatus.CLARIFYING: frozenset(
        {
            PlanningStatus.AWAITING_CONFIRMATION,
            PlanningStatus.PLANNING,
            PlanningStatus.FAILED,
            PlanningStatus.CANCELLED,
        }
    ),
    PlanningStatus.AWAITING_CONFIRMATION: frozenset(
        {
            PlanningStatus.CLARIFYING,
            PlanningStatus.PLANNING,
            PlanningStatus.FAILED,
            PlanningStatus.CANCELLED,
        }
    ),
    PlanningStatus.PLANNING: frozenset(
        {
            PlanningStatus.UNDER_REVIEW,
            PlanningStatus.FAILED,
            PlanningStatus.CANCELLED,
        }
    ),
    PlanningStatus.UNDER_REVIEW: frozenset(
        {
            PlanningStatus.PLAN_READY,
            PlanningStatus.PLANNING,
            PlanningStatus.REVIEW_LIMIT_REACHED,
            PlanningStatus.FAILED,
            PlanningStatus.CANCELLED,
        }
    ),
    PlanningStatus.PLAN_READY: frozenset(),
    PlanningStatus.REVIEW_LIMIT_REACHED: frozenset(),
    PlanningStatus.FAILED: frozenset(),
    PlanningStatus.CANCELLED: frozenset(),
}


def source_statuses(target: PlanningStatus) -> frozenset[PlanningStatus]:
    return frozenset(
        source
        for source, targets in PLANNING_TRANSITIONS.items()
        if target in targets
    )


class CreatePlanningSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=8000)
    clarifier_provider: AgentProvider | None = None
    planner_provider: AgentProvider | None = None
    reviewer_provider: AgentProvider | None = None
    max_review_turns: int | None = Field(default=None, ge=1, le=10)


class PlanningMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class PlanningMessageFinding(BaseModel):
    """A finding as the reviewer raised it in one round.

    Distinct from PlanningFinding, which is the ledger's current view of the
    same finding across every round. This is the point-in-time record, so it
    carries no status: what the round said, not what became of it.
    """

    finding_id: str
    severity: str
    text: str


class PlanningMessageFindingResponse(BaseModel):
    finding_id: str
    status: str
    rationale: str


class PlanningMessage(BaseModel):
    sequence: int
    role: PlanningRole
    text: str
    questions: list[str] = []
    revision: int | None = None
    approved: bool | None = None
    findings: list[PlanningMessageFinding] = []
    finding_responses: list[PlanningMessageFindingResponse] = []
    # The raw container log is fetched per message, not inlined here: the page
    # polls this payload every two seconds and the logs run to many kilobytes.
    has_raw_output: bool = False
    # The model that produced this turn, as recorded when it ran. Empty on a
    # turn no model produced, and on turns stored before the column existed.
    model: str = ""
    created_at: str


class PlanningMessageRaw(BaseModel):
    sequence: int
    role: PlanningRole
    raw_output: str


class PlanningFinding(BaseModel):
    finding_id: str
    severity: str
    text: str
    status: FindingStatus
    planner_response: str = ""
    raised_in_round: int
    last_seen_round: int


class PlanComponent(BaseModel):
    name: str
    responsibility: str = ""


class PlanRisk(BaseModel):
    severity: str = "medium"
    text: str


class ReviewerOutcome(BaseModel):
    approved: bool
    rounds: int
    summary: str = ""
    outstanding_findings: list[PlanningFinding] = []


class PlanSpec(BaseModel):
    title: str
    scope: str
    approach: str
    components: list[PlanComponent] = []
    risks: list[PlanRisk] = []
    open_questions: list[str] = []
    reviewer_outcome: ReviewerOutcome
    plan_markdown: str
    confirmed_understanding: bool
    generated_at: str


class PlanningSession(BaseModel):
    id: str
    project_id: str
    project_name: str
    sandbox_id: str
    title: str
    status: PlanningStatus
    feature_status: FeatureStatus
    turn_state: PlanningTurnState
    clarifier_provider: AgentProvider
    planner_provider: AgentProvider
    reviewer_provider: AgentProvider
    max_review_turns: int
    review_turn: int
    plan_revision: int
    confirmed: bool
    understanding_summary: str = ""
    failure_reason: str = ""
    created_at: str
    updated_at: str
    settled_at: str | None = None


class PlanningSessionDetail(PlanningSession):
    feature_brief: str = ""
    messages: list[PlanningMessage] = []
    findings: list[PlanningFinding] = []
    plan_spec: PlanSpec | None = None


class PlanningSessionsResponse(BaseModel):
    count: int
    sessions: list[PlanningSession]
