"""Finding the container that is running one claimed turn.

Every long turn now runs on a background thread (`app.jobs`) and reports
progress as events on the row it claimed. That tells a reader *that* work is
happening; the container's own output is what tells them *what* the assigned
model is doing. Each phase labels its container differently, so this maps a
job back to the Docker filter that finds it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from docker.client import DockerClient
from docker.errors import DockerException

from app.controller.store import ControllerStore
from app.previews.service import LABEL_CONTROLLER_MANAGED, LABEL_KIND
from app.planning.runner import LABEL_ROLE, LABEL_SESSION_ID
from app.tasks.runner import LABEL_TASK_ID


TURN_KINDS = ("context", "delegation", "run", "review")

#: The event kind each phase writes its progress under.
EVENT_KINDS: Mapping[str, str] = {
    "context": "context.progress",
    "delegation": "delegation.progress",
    "run": "run.progress",
    "review": "review.progress",
}

#: A progress step that means the turn is over, either way. A work-item run
#: ends on `awaiting_decision`: the container is gone and there is nothing
#: left to stream, even though the run row stays RUNNING until a person
#: accepts or rejects it.
TERMINAL_STEPS = frozenset({"settled", "awaiting_decision", "failed"})


class TurnNotFound(Exception):
    """The job id does not name a turn this session can see."""


@dataclass(frozen=True)
class TurnLocator:
    """How to recognise one turn's containers and its progress events."""

    event_kind: str
    labels: dict[str, str]

    def filters(self) -> dict[str, Any]:
        labels = [f"{LABEL_CONTROLLER_MANAGED}=true"]
        labels += [f"{name}={value}" for name, value in self.labels.items()]
        return {"label": labels}


def locate(
    store: ControllerStore,
    kind: str,
    job_id: str,
    *,
    session_id: str,
) -> TurnLocator:
    """Resolve a job to its container labels, or raise TurnNotFound.

    Scoping is by construction: a context, run, or review is only reachable
    when it really belongs to `session_id`, so a job id from another session
    cannot be used to read its logs.
    """
    if kind not in EVENT_KINDS:
        raise TurnNotFound(f"Unknown turn kind '{kind}'")
    event_kind = EVENT_KINDS[kind]

    if kind == "context":
        row = store.implementation_context(job_id)
        if row is None or str(row["session_id"]) != session_id:
            raise TurnNotFound("Implementation context was not found")
        return TurnLocator(
            event_kind,
            {
                LABEL_KIND: "planning",
                LABEL_SESSION_ID: session_id,
                LABEL_ROLE: "implementation_context",
            },
        )

    if kind == "delegation":
        # Decomposition has no row until it succeeds, so there is nothing to
        # check the job id against. The session label below still confines the
        # container search to this session's turns.
        return TurnLocator(
            event_kind,
            {
                LABEL_KIND: "planning",
                LABEL_SESSION_ID: session_id,
                LABEL_ROLE: "delegator",
            },
        )

    if kind == "run":
        run = store.work_item_run(job_id)
        if run is None:
            raise TurnNotFound("Work item run was not found")
        delegation = store.delegation(str(run["delegation_id"]))
        if delegation is None or str(delegation["session_id"]) != session_id:
            raise TurnNotFound("Work item run was not found")
        task_id = str(run.get("task_id") or "")
        if not task_id:
            raise TurnNotFound("Work item run has no task")
        return TurnLocator(
            event_kind,
            {LABEL_KIND: "coding-turn", LABEL_TASK_ID: task_id},
        )

    review = store.delegation_review(job_id)
    if review is None:
        raise TurnNotFound("Integration review was not found")
    delegation_id = str(review["delegation_id"])
    delegation = store.delegation(delegation_id)
    if delegation is None or str(delegation["session_id"]) != session_id:
        raise TurnNotFound("Integration review was not found")
    return TurnLocator(
        event_kind,
        {
            LABEL_KIND: "planning",
            # The reviewer turn passes the delegation id as its session id.
            LABEL_SESSION_ID: delegation_id,
            LABEL_ROLE: "integration_reviewer",
        },
    )


def running_containers(
    docker_client: DockerClient,
    locator: TurnLocator,
) -> list[Any]:
    """Containers currently running this turn. Empty when none are alive."""
    try:
        return docker_client.containers.list(filters=locator.filters())
    except DockerException:
        return []


__all__ = [
    "EVENT_KINDS",
    "TERMINAL_STEPS",
    "TURN_KINDS",
    "TurnLocator",
    "TurnNotFound",
    "locate",
    "running_containers",
]
