from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Where one unit of coding-agent work sits between start and settlement.

    OPEN is the only status an agent's own actions can reach: the agent
    commits to the task branch and the controller, having read that branch
    itself, is the party that moves the task on. Every later status is a
    controller decision, so no file an agent writes can produce one.
    """

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
    # open -> rejected exists so a coding agent that never commits cannot hold
    # its sandbox's only task slot forever. Every non-terminal status sits
    # inside one_open_task_per_sandbox, so a status with no exit is a deadlock.
    TaskStatus.OPEN: frozenset({TaskStatus.REPORTED, TaskStatus.REJECTED}),
    TaskStatus.REPORTED: frozenset({TaskStatus.PREVIEWING}),
    TaskStatus.PREVIEWING: frozenset({TaskStatus.REVIEW, TaskStatus.FAILED}),
    TaskStatus.REVIEW: frozenset(
        {TaskStatus.ACCEPTED, TaskStatus.REJECTED, TaskStatus.FAILED}
    ),
    TaskStatus.ACCEPTED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


def source_statuses(target: TaskStatus) -> frozenset[TaskStatus]:
    return frozenset(
        source for source, targets in TASK_TRANSITIONS.items() if target in targets
    )


class StartTaskRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=200)


class ReportTaskRequest(BaseModel):
    """The agent's completion claim. A hint only; nothing here changes state.

    summary is agent-authored text. It reaches the audit event log and stops
    there. Whether the task advances is decided by reading the task branch.
    """

    summary: str = Field(default="", max_length=2000)


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
