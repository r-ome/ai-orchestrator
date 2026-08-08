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


def reconcile_controller_state(store: ControllerStore) -> dict[str, int]:
    client = None
    counts = {
        "sandboxes": 0,
        "agents": 0,
        "previews": 0,
        "planning": 0,
        "missing": 0,
        "unexpected": 0,
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
    try:
        client = docker.from_env()
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
