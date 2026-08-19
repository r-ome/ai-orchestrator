"""Run a delegation's ready work items to the end without a person in the loop.

Every piece this needs already existed. `execute_run` takes one item from
coding turn through verification, one focused repair, and the fast-forward
merge, with no human step on the success path. `service.view` derives which
items are ready, which are blocked, and which failed. Nothing joined the two:
each item had to be started by hand, so the loop was a person clicking.

This module is that loop, and almost all of its safety is inherited rather
than invented:

- A failure cannot spread. `graph.item_states` marks the dependents of a
  failed item BLOCKED, never READY, so the driver stops offering that subtree
  while unrelated items keep running. The driver therefore does not need a
  consecutive-failure rule; the graph already contains the blast radius.
- A failed item is never retried here. `claim_run` would accept it, because a
  person re-running a failure is a real workflow. The driver takes only READY
  items, which is what makes "move on and report" the behaviour rather than a
  retry storm.
- Termination is a property of the graph, not a countdown. The loop ends when
  nothing is READY, which happens when every item is either COMPLETED or
  BLOCKED behind a failure.

Two things the graph cannot give, so they are here: a wall-clock cap, because
a coding turn can wait CODING_TURN_TIMEOUT_SECONDS (1800) and several items in
sequence can outlive any reasonable absence; and a no-progress guard, because
a claim that fails before it records a run would leave the item READY and spin
this loop forever.

Serial on purpose. One sandbox is one volume mounted at /project, one git
working tree, and `one_open_task_per_sandbox` is a unique index. Two runs at
once would fight over HEAD, and the second `--ff-only` merge would be refused
as diverged even for items that share no files. Parallel execution is a
separate design that has to replace the merge model first.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from docker.client import DockerClient

from app.controller.store import ControllerStore
from app.delegation import service
from app.delegation.config import DriverSettings, get_driver_settings
from app.delegation.execution import start_run
from app.delegation.models import (
    DelegationStatus,
    RunStatus,
    StartRunRequest,
    WorkItemState,
)
from app.delegation.routing import RoutingSettings
from app.delegation.verification import VerificationSettings
from app.tasks.config import CodingTurnSettings


logger = logging.getLogger(__name__)

# The statuses from which the driver may start more work. A delegation halted
# or abandoned while the loop was inside a coding turn must not have the next
# item started on top of it, so this is re-read every pass rather than once.
_DRIVABLE = frozenset({DelegationStatus.READY, DelegationStatus.RUNNING})

#: Progress is recorded against the delegation id, so the events socket streams
#: a drive the same way it streams a single turn. `settled` is a terminal step,
#: which is what closes the reader's console.
EVENT_KIND = "drive.progress"


def _progress(
    store: ControllerStore,
    delegation_id: str,
    sandbox_id: str,
    *,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    store.progress_event(
        sandbox_id=sandbox_id,
        run_id=delegation_id,
        kind=EVENT_KIND,
        step=step,
        message=message,
        level=level,
    )


@dataclass
class DriveOutcome:
    """What the driver did, for the person reading it afterwards."""

    delegation_id: str
    attempted: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    status: DelegationStatus = DelegationStatus.RUNNING
    stopped_because: str = ""


def drive_delegation(
    docker_client: DockerClient,
    store: ControllerStore,
    delegation_id: str,
    *,
    settings: CodingTurnSettings,
    driver_settings: DriverSettings | None = None,
    routing_settings: RoutingSettings | None = None,
    verification_settings: VerificationSettings | None = None,
    session_id: str | None = None,
    project_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DriveOutcome:
    """Run ready items one at a time until the graph drains or a cap trips.

    `clock` is injected so a test can exhaust the wall-clock cap without
    waiting for it.
    """
    driver = driver_settings or get_driver_settings()
    deadline = clock() + driver.max_seconds
    outcome = DriveOutcome(delegation_id=delegation_id)
    attempted: set[str] = set()
    sandbox_id = ""

    while True:
        view = service.view(
            store,
            delegation_id,
            session_id=session_id,
            project_name=project_name,
        )
        outcome.status = view.delegation.status
        if not sandbox_id:
            sandbox_id = view.delegation.sandbox_id
            _progress(
                store,
                delegation_id,
                sandbox_id,
                step="started",
                message=(
                    f"Driving {len(view.items)} work item(s), "
                    f"cap {driver.max_seconds}s"
                ),
            )
        if outcome.status not in _DRIVABLE:
            # Somebody halted or abandoned it, or the last run completed the
            # graph. Either way this is a settled end, not a stop.
            outcome.stopped_because = f"delegation is '{outcome.status.value}'"
            break

        ready = _next_keys(view)
        if not ready:
            outcome.stopped_because = "no work item is ready"
            break

        key = ready[0]
        if key in attempted:
            # The item was offered, started, and came back READY. Whatever
            # refused it will refuse it again, so looping cannot help.
            outcome.stopped_because = (
                f"work item '{key}' did not leave the ready state after a run"
            )
            _halt(
                store,
                delegation_id,
                outcome,
                session_id=session_id,
                project_name=project_name,
            )
            break

        if clock() >= deadline:
            outcome.stopped_because = (
                f"wall-clock cap of {driver.max_seconds}s reached with "
                f"'{key}' still to run"
            )
            _halt(
                store,
                delegation_id,
                outcome,
                session_id=session_id,
                project_name=project_name,
            )
            break

        attempted.add(key)
        outcome.attempted.append(key)
        _progress(
            store,
            delegation_id,
            sandbox_id,
            step="item_started",
            message=f"Running work item '{key}'",
        )
        try:
            result = start_run(
                docker_client,
                settings,
                store,
                delegation_id,
                key,
                StartRunRequest(),
                routing_settings=routing_settings,
                verification_settings=verification_settings,
                session_id=session_id,
                project_name=project_name,
            )
        except service.DelegationOperationError as error:
            # A run that fails settles its own row, and the item then reads
            # FAILED rather than READY. Reaching here means the claim itself
            # was refused, which is a condition the next pass would hit again.
            outcome.stopped_because = f"work item '{key}' could not start: {error.detail}"
            _halt(
                store,
                delegation_id,
                outcome,
                session_id=session_id,
                project_name=project_name,
            )
            break

        _progress(
            store,
            delegation_id,
            sandbox_id,
            step="item_finished",
            message=f"Work item '{key}' {result.run_status.value}",
            level="info" if result.run_status is RunStatus.SUCCEEDED else "warning",
        )

    _record_final_states(
        store,
        delegation_id,
        outcome,
        session_id=session_id,
        project_name=project_name,
    )
    if sandbox_id:
        # `settled` is terminal, so this is what closes the reader's console.
        # It is sent after the final states are read, so the message the reader
        # is left looking at is the one that describes the whole drive.
        _progress(
            store,
            delegation_id,
            sandbox_id,
            step="settled",
            message=(
                f"Stopped: {outcome.stopped_because}. "
                f"{len(outcome.completed)} completed, {len(outcome.failed)} failed, "
                f"{len(outcome.blocked)} blocked"
            ),
            level="info" if not outcome.failed else "warning",
        )
    logger.info(
        "delegation %s driver stopped: %s (completed=%d failed=%d blocked=%d)",
        delegation_id,
        outcome.stopped_because,
        len(outcome.completed),
        len(outcome.failed),
        len(outcome.blocked),
    )
    return outcome


def _next_keys(view: service.DelegationView) -> list[str]:
    """Ready item keys, earliest wave first.

    `view.ready` is sorted by key, which is a display order. Running in wave
    order keeps the merge sequence close to the order the delegator intended,
    which matters because each item is cut from the branch the last one left.
    """
    ready = [entry for entry in view.items if entry.state is WorkItemState.READY]
    ready.sort(key=lambda entry: (entry.wave, entry.item.position))
    return [entry.item.key for entry in ready]


def _record_final_states(
    store: ControllerStore,
    delegation_id: str,
    outcome: DriveOutcome,
    *,
    session_id: str | None,
    project_name: str | None,
) -> None:
    view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    outcome.status = view.delegation.status
    outcome.completed = [
        entry.item.key
        for entry in view.items
        if entry.state is WorkItemState.COMPLETED
    ]
    outcome.failed = [
        entry.item.key for entry in view.items if entry.state is WorkItemState.FAILED
    ]
    outcome.blocked = [
        entry.item.key for entry in view.items if entry.state is WorkItemState.BLOCKED
    ]
    if outcome.status is not DelegationStatus.RUNNING or not outcome.failed:
        return
    # The graph drained but something failed, so `_complete_if_finished` left
    # the delegation running. Nothing else will move it, and a delegation that
    # sits in `running` with no runnable work reads as still working.
    outcome.stopped_because = (
        f"{len(outcome.failed)} work item(s) failed: {', '.join(outcome.failed)}"
        + (
            f"; {len(outcome.blocked)} blocked behind them: "
            f"{', '.join(outcome.blocked)}"
            if outcome.blocked
            else ""
        )
    )
    _halt(
        store,
        delegation_id,
        outcome,
        session_id=session_id,
        project_name=project_name,
    )
    outcome.status = DelegationStatus.HALTED


def _halt(
    store: ControllerStore,
    delegation_id: str,
    outcome: DriveOutcome,
    *,
    session_id: str | None,
    project_name: str | None,
) -> None:
    """Halt the delegation, keeping the driver's reason as the halt reason.

    Best-effort by design, for two reasons that both mean the same thing: the
    driver has stopped either way, and the stored status should describe the
    delegation rather than the driver.

    A delegation still READY has run nothing, and READY has no transition to
    HALTED — only to RUNNING. Leaving it READY is the truthful outcome, and it
    stays re-drivable. A delegation another writer already settled keeps that
    writer's status for the same reason. `stopped_because` carries the driver's
    account in both cases.
    """
    try:
        view = service.transition(
            store,
            delegation_id,
            DelegationStatus.HALTED,
            error=outcome.stopped_because,
            session_id=session_id,
            project_name=project_name,
        )
    except service.DelegationOperationError as error:
        logger.info(
            "delegation %s could not be halted by the driver: %s",
            delegation_id,
            error.detail,
        )
        return
    outcome.status = view.delegation.status
