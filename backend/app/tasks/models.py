from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.models import AgentProvider


class TaskStatus(StrEnum):
    """Where one unit of coding-agent work sits between start and settlement.

    An agent cannot choose a status. The controller opens the task, verifies
    its branch, and can reopen a reported task for one focused repair.
    """

    PREPARING = "preparing"
    OPEN = "open"
    REPORTED = "reported"
    PREVIEWING = "previewing"
    REVIEW = "review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


# Must stay identical to the one_open_task_per_sandbox partial index in
# app/controller/store.py: the index is what enforces the single open task,
# and this set is what the API reads back.
OPEN_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PREPARING,
        TaskStatus.OPEN,
        TaskStatus.REPORTED,
        TaskStatus.PREVIEWING,
        TaskStatus.REVIEW,
    }
)

TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.ACCEPTED, TaskStatus.REJECTED, TaskStatus.FAILED}
)

# Only used for task rows written before base_branch was recorded. New tasks
# always carry the branch the controller read from the sandbox at start.
DEFAULT_BASE_BRANCH = "main"

# The whole status machine, in one place. Anything absent here is
# unreachable, because transition_task derives the guarded UPDATE's source
# statuses from this table and never accepts them from a caller.
TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    # Preparing is controller-only. An agent never sees the task until the
    # baseline facts and branch have been verified.
    TaskStatus.PREPARING: frozenset({TaskStatus.OPEN, TaskStatus.FAILED}),
    # open -> rejected exists so a coding agent that never commits cannot hold
    # its sandbox's only task slot forever. Every non-terminal status sits
    # inside one_open_task_per_sandbox, so a status with no exit is a deadlock.
    TaskStatus.OPEN: frozenset({TaskStatus.REPORTED, TaskStatus.REJECTED}),
    # reported -> open is the controller-directed focused repair path.
    # reported -> review is the non-preview path, taken by a delegated run
    # whose branch the controller verified and whose configured verification
    # commands passed. Many units of delegated work — a shared helper, a
    # migration, a refactor — have nothing meaningful to preview, and a
    # mid-graph item can leave the application temporarily unbuildable, so
    # requiring a preview stack would make them unacceptable rather than
    # unverified. An agent-driven task still goes through previewing.
    #
    # reported -> rejected exists for the same reason as open -> rejected: a
    # status whose only exit is unavailable holds the sandbox's one task slot
    # forever.
    TaskStatus.REPORTED: frozenset(
        {
            TaskStatus.OPEN,
            TaskStatus.PREVIEWING,
            TaskStatus.REVIEW,
            TaskStatus.REJECTED,
        }
    ),
    TaskStatus.PREVIEWING: frozenset({TaskStatus.REVIEW, TaskStatus.FAILED}),
    TaskStatus.REVIEW: frozenset(
        {TaskStatus.ACCEPTED, TaskStatus.REJECTED, TaskStatus.FAILED}
    ),
    TaskStatus.ACCEPTED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


def source_statuses(target: TaskStatus) -> frozenset[TaskStatus]:
    sources = frozenset(
        source for source, targets in TASK_TRANSITIONS.items() if target in targets
    )
    if target is TaskStatus.OPEN:
        # PREPARING -> OPEN also writes the verified base fields. The store's
        # complete_task_preparation method owns that controller-only move.
        return sources.difference({TaskStatus.PREPARING})
    return sources


class StartTaskRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=200)


class ReportTaskRequest(BaseModel):
    """The agent's completion claim. A hint only; nothing here changes state.

    summary is agent-authored text. It reaches the audit event log and stops
    there. Whether the task advances is decided by reading the task branch.
    """

    summary: str = Field(default="", max_length=2000)


class RunTaskRequest(BaseModel):
    """Run one headless coding turn for an open task.

    The prompt is supplied per run rather than stored on the task, so a task
    stays a branch-backed unit of work and does not have to know whether a
    person or a delegation is driving it.
    """

    prompt: str = Field(min_length=1, max_length=200_000)
    provider: AgentProvider = AgentProvider.CLAUDE
    model: str | None = Field(default=None, max_length=100)


class TurnUsageView(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None


class TaskRunResponse(BaseModel):
    """What one headless turn did, and what the branch says about it.

    `turn_status` is the provider's account. `committed` is the controller's,
    read from git. They can disagree, and when they do the branch is right.
    """

    task: Task
    turn_status: str
    turn_error: str | None = None
    committed: bool
    detail: str = ""
    model: str = ""
    usage: TurnUsageView = Field(default_factory=TurnUsageView)
    duration_ms: int | None = None
    exit_code: int | None = None
    tool_calls: int = 0
    failed_tool_calls: int = 0
    #: The turn's own structured report, when it produced one. Recorded, never
    #: trusted: acceptance rests on git, not on this.
    result: dict[str, Any] | None = None


class Task(BaseModel):
    id: str
    sandbox_id: str
    agent_run_id: str | None = None
    branch: str
    base_branch: str | None = None
    base_commit: str
    head_commit: str | None = None
    status: TaskStatus
    title: str = ""
    created_at: str
    updated_at: str
    settled_at: str | None = None


class TasksResponse(BaseModel):
    count: int
    tasks: list[Task]
