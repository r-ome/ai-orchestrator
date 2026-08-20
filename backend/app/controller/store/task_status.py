"""Task status vocabulary and its guarded stored transition.

This module lives in the store because previews and tasks both read this stored
vocabulary. Keeping it in tasks put previews -> tasks into the app import cycle.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.controller.store import ControllerStore


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


def transition_task(
    controller_store: "ControllerStore",
    *,
    task_id: str,
    to_status: TaskStatus,
    head_commit: str | None = None,
) -> bool:
    """The only way a task's status changes. Sources come from TASK_TRANSITIONS.

    Callers name a destination, never a source, so a transition the table does
    not draw cannot be requested. The store turns the sources into the UPDATE's
    WHERE clause, which makes the check atomic rather than a read-then-write.
    """
    return controller_store.advance_task_status(
        task_id=task_id,
        from_statuses=[status.value for status in source_statuses(to_status)],
        to_status=to_status.value,
        head_commit=head_commit,
        settled=to_status in TERMINAL_TASK_STATUSES,
    )
