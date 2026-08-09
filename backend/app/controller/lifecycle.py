import asyncio
from contextlib import suppress
from typing import Any

import docker
from docker.errors import DockerException

from app.agents.service import LABEL_RUN_ID as AGENT_RUN_ID
from app.controller.config import ControllerSettings
from app.controller.store import ControllerStore
from app.previews.service import (
    LABEL_CONTROLLER_MANAGED,
    LABEL_KIND,
    LABEL_RUN_ID,
    expire_previews,
)
from app.projects.service import (
    LABEL_CREATED_AT as PROJECT_CREATED_AT,
    LABEL_MANAGED as PROJECT_MANAGED,
    LABEL_NAME as PROJECT_NAME,
    LABEL_SANDBOX_ID,
    LABEL_SOURCE as PROJECT_SOURCE,
    project_id,
)


_RESTART_REASON = "The backend restarted while this turn was running"


def _settle_interrupted_turns(store: ControllerStore) -> tuple[int, list[str]]:
    """Fail every row a background turn left in flight.

    Without this a killed process leaves a context stuck on 'generating',
    which the `one_generating_context_per_session` index then reads as a live
    turn — the session can never generate a context again. Work item runs and
    integration reviews have the same shape.

    Returns the count settled and the task ids the failed runs abandoned, for
    `_reject_abandoned_tasks` to collect once a Docker client exists.
    """
    interrupted = store.interrupted_turns()
    settled = 0
    abandoned: list[str] = []
    for row in interrupted["implementation_contexts"]:
        store.settle_implementation_context(
            str(row["id"]),
            to_status="failed",
            changes={"error": _RESTART_REASON},
        )
        settled += 1
    for row in interrupted["delegation_reviews"]:
        store.settle_delegation_review(
            str(row["id"]),
            to_status="failed",
            error=_RESTART_REASON,
        )
        settled += 1
    for row in interrupted["work_item_runs"]:
        store.settle_work_item_run(
            str(row["id"]),
            to_status="failed",
            changes={"error": _RESTART_REASON, "failure_kind": "unknown"},
        )
        settled += 1
        task_id = str(row["task_id"] or "")
        if task_id:
            abandoned.append(task_id)
    return settled, abandoned


def _reject_abandoned_tasks(
    client: Any,
    store: ControllerStore,
    task_ids: list[str],
) -> int:
    """Remove the task branch each interrupted run left behind.

    Failing the run is not enough on its own. Its task stays in an open status,
    and a sandbox allows only one open task, so `start_task` refuses every
    later run on that sandbox — the delegation cannot make progress again
    without a person editing the database.

    Only tasks from runs `_settle_interrupted_turns` just failed are passed in.
    A run whose turn finished is never in that set, so no branch holding a
    verified commit is deleted here.
    """
    # Imported here rather than at module scope: app.tasks.service pulls in the
    # preview and project services, and this module is imported during startup
    # before those are needed.
    from app.tasks.service import TaskOperationError, reject_task

    rejected = 0
    for task_id in task_ids:
        try:
            reject_task(client, store, task_id)
        except (TaskOperationError, DockerException):
            # A branch left behind is recoverable; a startup that dies here is
            # not. The task keeps its row, and the next reconciliation retries.
            continue
        rejected += 1
    return rejected


def reconcile_controller_state(store: ControllerStore) -> dict[str, int]:
    client = None
    counts = {
        "sandboxes": 0,
        "agents": 0,
        "previews": 0,
        "planning": 0,
        "turns": 0,
        "missing": 0,
        "unexpected": 0,
        "abandoned_tasks": 0,
    }
    for session in store.running_planning_sessions():
        if store.advance_planning_status(
            session_id=str(session["id"]),
            from_statuses=(str(session["status"]),),
            to_status="failed",
            settled=True,
            failure_reason="The backend restarted while this turn was running",
        ):
            counts["planning"] += 1
        store.release_planning_turn(str(session["id"]))
    counts["turns"], abandoned_tasks = _settle_interrupted_turns(store)
    try:
        client = docker.from_env()
        counts["abandoned_tasks"] = _reject_abandoned_tasks(
            client,
            store,
            abandoned_tasks,
        )
        containers = client.containers.list(
            all=True,
            filters={"label": f"{LABEL_CONTROLLER_MANAGED}=true"},
        )
        by_run: dict[str, list[Any]] = {}
        for container in containers:
            labels = (container.attrs.get("Config") or {}).get("Labels") or {}
            run_id = labels.get(LABEL_RUN_ID) or labels.get(AGENT_RUN_ID)
            if run_id:
                by_run.setdefault(run_id, []).append(container)

        project_volumes = client.volumes.list(
            filters={"label": f"{PROJECT_MANAGED}=true"}
        )
        volumes_by_name = {volume.name: volume for volume in project_volumes}
        known_volume_names: set[str] = set()
        for sandbox in store.sandboxes():
            volume_name = str(sandbox["volume_name"])
            known_volume_names.add(volume_name)
            if volume_name not in volumes_by_name:
                store.update_sandbox_status(str(sandbox["id"]), "missing")
                counts["missing"] += 1
            else:
                counts["sandboxes"] += 1
        for volume in project_volumes:
            if volume.name in known_volume_names:
                continue
            labels = volume.attrs.get("Labels") or {}
            sandbox_id = labels.get(LABEL_SANDBOX_ID)
            source_path = labels.get(PROJECT_SOURCE, "")
            project_name = labels.get(PROJECT_NAME, volume.name)
            if not sandbox_id or not source_path:
                continue
            store.register_sandbox(
                sandbox_id=sandbox_id,
                project_id=project_id(source_path),
                project_name=project_name,
                source_path=source_path,
                volume_name=volume.name,
                status="discovered",
                created_at=labels.get(PROJECT_CREATED_AT, ""),
            )
            store.event(
                sandbox_id=sandbox_id,
                run_id=None,
                kind="controller.discovered_sandbox",
                payload={"volume_name": volume.name},
            )
            counts["unexpected"] += 1

        known: set[str] = set()
        for run in store.active_agents():
            run_id = str(run["id"])
            known.add(run_id)
            resources = by_run.get(run_id, [])
            if not resources:
                store.update_agent_run(run_id, status="missing")
                counts["missing"] += 1
                continue
            container = resources[0]
            status = "running" if container.status == "running" else container.status
            store.update_agent_run(
                run_id,
                status=status,
                container_id=container.id,
            )
            counts["agents"] += 1

        for run in store.active_previews():
            run_id = str(run["id"])
            known.add(run_id)
            resources = by_run.get(run_id, [])
            if not resources:
                store.update_preview_run(run_id, status="missing")
                counts["missing"] += 1
                continue
            status = "running" if any(item.status == "running" for item in resources) else "failed"
            store.update_preview_run(run_id, status=status)
            counts["previews"] += 1

        for run_id, resources in by_run.items():
            if run_id in known:
                continue
            labels = (resources[0].attrs.get("Config") or {}).get("Labels") or {}
            store.event(
                sandbox_id=labels.get("orchestrator.sandbox.id"),
                run_id=run_id,
                kind="controller.unexpected_resource",
                payload={
                    "kind": labels.get(LABEL_KIND, "unknown"),
                    "containers": [item.id for item in resources],
                },
            )
            counts["unexpected"] += 1
        expire_previews(client, store)
    except DockerException:
        return counts
    finally:
        if client is not None:
            client.close()
    return counts


async def expiry_loop(
    store: ControllerStore,
    settings: ControllerSettings,
) -> None:
    while True:
        await asyncio.sleep(settings.expiry_poll_seconds)
        await asyncio.to_thread(_expire_once, store)


def _expire_once(store: ControllerStore) -> None:
    client = None
    try:
        client = docker.from_env()
        expire_previews(client, store)
    except DockerException:
        return
    finally:
        if client is not None:
            client.close()


async def cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
