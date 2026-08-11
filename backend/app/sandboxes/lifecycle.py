"""Persisted admission for per-sandbox lifecycle mutations."""

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from docker.client import DockerClient
from docker.errors import NotFound

from app.controller.store import (
    ControllerStore,
    SandboxAdmissionError,
    SandboxLeaseBlockedByWriterError,
    SandboxLeaseHeldError,
)


def lifecycle_conflict_detail(error: SandboxAdmissionError) -> dict[str, Any]:
    detail: dict[str, Any] = {"message": str(error)}
    if isinstance(error, SandboxLeaseBlockedByWriterError):
        detail["blocking_writer"] = {
            "class": error.writer_class,
            "id": error.writer_id,
        }
    elif isinstance(error, SandboxLeaseHeldError):
        detail["blocking_lease"] = {
            "operation": str(error.lease["operation"]),
            "operation_id": str(error.lease["operation_id"]),
        }
    return detail


@contextmanager
def lifecycle_lease(
    store: ControllerStore,
    sandbox_id: str,
    operation: str,
    *,
    docker_client: DockerClient | None = None,
    stop_blocking_previews: bool = False,
    operation_id: str | None = None,
    owner: str | None = None,
    allow_writers: bool = False,
) -> Iterator[dict[str, Any] | None]:
    """Acquire and always release one sandbox's lifecycle lease.

    The optional preview stop is explicit. It uses the existing task-preview
    teardown path and retries the atomic admission after teardown completes.
    """
    claimed_operation_id = operation_id or uuid4().hex
    claimed_owner = owner or f"{socket.gethostname()}:{os.getpid()}"
    try:
        lease = store.acquire_sandbox_lease(
            sandbox_id=sandbox_id,
            operation=operation,
            operation_id=claimed_operation_id,
            owner=claimed_owner,
            allow_writers=allow_writers,
        )
    except SandboxLeaseBlockedByWriterError as error:
        preview = next(
            (
                writer
                for writer in error.writers
                if str(writer["writer_class"]) == "preview"
            ),
            None,
        )
        if not stop_blocking_previews or preview is None or docker_client is None:
            raise
        _stop_blocking_preview(
            docker_client,
            store,
            sandbox_id=sandbox_id,
            preview_id=str(preview["writer_id"]),
        )
        lease = store.acquire_sandbox_lease(
            sandbox_id=sandbox_id,
            operation=operation,
            operation_id=claimed_operation_id,
            owner=claimed_owner,
            allow_writers=allow_writers,
        )
    try:
        yield lease
    finally:
        if lease is not None:
            store.release_sandbox_lease(sandbox_id, claimed_operation_id)


@contextmanager
def project_mirror_lock(
    store: ControllerStore,
    project_id: str,
    operation: str,
    *,
    operation_id: str | None = None,
    owner: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Serialize mirror creation, validation, and fetch for one project.

    Callers holding both locks must enter ``lifecycle_lease`` first and this
    context second.  Do not hold this lock across workspace cloning or other
    sandbox work.
    """
    claimed_operation_id = operation_id or uuid4().hex
    claimed_owner = owner or f"{socket.gethostname()}:{os.getpid()}"
    lock = store.acquire_project_mirror_lock(
        project_id=project_id,
        operation=operation,
        operation_id=claimed_operation_id,
        owner=claimed_owner,
    )
    try:
        yield lock
    finally:
        store.release_project_mirror_lock(project_id, claimed_operation_id)


def _stop_blocking_preview(
    docker_client: DockerClient,
    store: ControllerStore,
    *,
    sandbox_id: str,
    preview_id: str,
) -> None:
    preview = store.preview_run(preview_id)
    sandbox = store.sandbox(sandbox_id)
    if preview is None or sandbox is None:
        return
    task_id = str(preview.get("task_id") or "")
    if task_id:
        task_row = store.task(task_id)
        if task_row is None:
            return
        # Keep task preview teardown in its single established helper.
        from app.tasks.models import Task
        from app.tasks.service import _stop_task_preview

        _stop_task_preview(
            docker_client,
            store,
            Task.model_validate(task_row),
            sandbox,
        )
        return

    # Live previews use the same stop_preview teardown called by the task
    # helper. There is no task row to pass to _stop_task_preview.
    from app.previews.service import stop_preview

    stop_preview(
        docker_client,
        store,
        (
            sandbox_id
            if sandbox.get("lifecycle_version") == "v1"
            else str(sandbox["project_name"])
        ),
        remove_data_volumes=True,
        status="stopped",
    )


def drain_sandbox_writers(
    docker_client: DockerClient,
    store: ControllerStore,
    sandbox_id: str,
) -> int:
    """Stop the writers present when destroy claimed its lease.

    This drains writer processes and active controller rows only. The complete
    sandbox resource sweep and tombstone remain Phase 5d work.
    """
    writers = store.active_writers(sandbox_id)
    if not writers:
        return 0

    # Stop the parent first. The destroy lease already prevents it from
    # claiming another child task while the current child is being stopped.
    for writer in writers:
        if str(writer["writer_class"]) != "delegation":
            continue
        store.transition_delegation(
            str(writer["writer_id"]),
            to_status="abandoned",
            from_statuses=(str(writer["status"]),),
            terminal=True,
            error="Sandbox destroy drained this delegation",
        )

    for writer in writers:
        if str(writer["writer_class"]) != "preview":
            continue
        _stop_blocking_preview(
            docker_client,
            store,
            sandbox_id=sandbox_id,
            preview_id=str(writer["writer_id"]),
        )

    for writer in writers:
        if str(writer["writer_class"]) != "agent_writer_session":
            continue
        session_id = str(writer["writer_id"])
        agent_run_id = str(writer.get("agent_run_id") or "")
        run = store.agent_run(agent_run_id) if agent_run_id else None
        container_id = str((run or {}).get("container_id") or "")
        if container_id:
            from app.agents.service import AgentOperationError, stop_agent

            try:
                stop_agent(
                    docker_client,
                    container_id,
                    controller_store=store,
                )
            except (AgentOperationError, NotFound) as error:
                if getattr(error, "status_code", 404) != 404:
                    raise
        store.close_agent_writer_session(session_id)

    task_ids = [
        str(writer["writer_id"])
        for writer in writers
        if str(writer["writer_class"]) == "task"
    ]
    if task_ids:
        from app.tasks.runner import LABEL_TASK_ID

        for task_id in task_ids:
            containers = docker_client.containers.list(
                all=True,
                filters={"label": f"{LABEL_TASK_ID}={task_id}"},
            )
            for container in containers:
                try:
                    container.remove(force=True)
                except NotFound:
                    pass
            task = store.task(task_id)
            if task is not None:
                store.advance_task_status(
                    task_id=task_id,
                    from_statuses=(str(task["status"]),),
                    to_status="failed",
                    settled=True,
                )

    remaining = store.active_writers(sandbox_id)
    if remaining:
        writer = remaining[0]
        raise RuntimeError(
            f"Could not drain {writer['writer_class']} writer "
            f"'{writer['writer_id']}' from sandbox '{sandbox_id}'"
        )
    return len(writers)
