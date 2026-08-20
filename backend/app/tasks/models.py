"""The Task: one coding-agent turn on a temporary branch cut from a sandbox.

The controller cuts the branch, the agent commits to it, the controller verifies
the branch against git, and then fast-forwards it (accept) or deletes it (reject).
Acceptance rests on what git shows, never on what the agent reports.

A Task belongs to a **sandbox**, not to a delegation. `tasks.sandbox_id` is its
only foreign key. Three things open one:

- a work item run, which records `work_item_runs.task_id`;
- a feature change request, which records `delegation_change_requests.task_id`;
- a direct `POST /tasks`, with no delegation involved at all.

The delegation side points *at* a Task; it does not own it. Reading the chain the
other way round is the mistake this docstring exists to prevent.

`one_open_task_per_sandbox` holds a sandbox to one open Task at a time. See
`OPEN_TASK_STATUSES` in `app/controller/store/task_status.py`, which must stay identical to that partial index.

Not a "sandbox change". `change` already names a different concept in
`app/delegation/`: a revision of a feature diff under review
(`FeatureChangeRequest`), which owns a Task through `task_id`. See CONTEXT.md.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.agents.models import AgentProvider
from app.controller.store.task_status import TaskStatus

# Only used for task rows written before base_branch was recorded. New tasks
# always carry the branch the controller read from the sandbox at start.
DEFAULT_BASE_BRANCH = "main"


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

    task: Task  # noqa: F821 - Pydantic resolves this same-module forward reference
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
