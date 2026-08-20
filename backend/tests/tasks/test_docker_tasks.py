import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker
import pytest
from conftest import register_ready_v1_sandbox

from app.controller.store import ControllerStore
from app.controller.store.task_status import TaskStatus, transition_task
from app.projects.models import ProjectRegistration
from app.tasks.models import ReportTaskRequest, StartTaskRequest
from app.tasks.service import (
    TaskOperationError,
    accept_task,
    get_task,
    reject_task,
    report_task_complete,
    start_task,
)

requires_docker = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_PREVIEW_TESTS") != "1",
    reason="set RUN_DOCKER_PREVIEW_TESTS=1 to use the local Docker daemon",
)

GIT_IMAGE = "alpine/git:latest"


def _run(client: Any, volume_name: str, script: str) -> str:
    output = client.containers.run(
        GIT_IMAGE,
        entrypoint=["sh", "-c"],
        command=[script],
        remove=True,
        network_disabled=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        volumes={volume_name: {"bind": "/project", "mode": "rw"}},
        # alpine/git declares VOLUME /git, so every run without a mount there
        # creates an empty anonymous volume that --rm does not reap. Measured:
        # this helper alone leaked 64 volumes per run of this file.
        tmpfs={"/git": "rw,nosuid,size=1m"},
    )
    return output.decode().strip()


@pytest.fixture
def sandbox_volume(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = docker.from_env()
    volume = client.volumes.create(name=f"orchestrator-task-test-{uuid4().hex[:12]}")
    _run(client, volume.name, "printf 'hello' > /project/index.html")
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="managed:project-1",
        volume_name=volume.name,
        created_at="2026-01-01T00:00:00Z",
        ready=True,
    )
    monkeypatch.setattr(
        "app.tasks.service.inspect_registered_project",
        lambda *_: project,
    )
    try:
        yield client, volume
    finally:
        volume.remove(force=True)


@pytest.fixture
def controller_store(tmp_path: Path, sandbox_volume: Any) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    _, volume = sandbox_volume
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="Sample Project",
        volume_name=volume.name,
        created_at="2026-01-01T00:00:00Z",
    )
    return store


@requires_docker
def test_a_committed_change_moves_the_task_to_reported(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume

    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project", title="Add a contact page"),
    )

    assert task.status is TaskStatus.OPEN
    assert _run(client, volume.name, "cd /project && git branch --show-current") == (
        task.branch
    )

    # The coding agent's work: a commit on the task branch.
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "cd /project\n"
            "printf 'contact' > contact.astro\n"
            "git add -A\n"
            'git commit -q -m "add contact page"\n'
        ),
    )

    reported = report_task_complete(
        client,
        controller_store,
        task.id,
        ReportTaskRequest(summary="done"),
    )

    assert reported.status is TaskStatus.REPORTED
    assert len(reported.head_commit) == 40
    assert reported.head_commit != reported.base_commit
    assert reported.head_commit == _run(
        client,
        volume.name,
        f'cd /project && git rev-parse "refs/heads/{task.branch}"',
    )


@requires_docker
def test_a_report_with_no_commit_leaves_the_task_open(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, _volume = sandbox_volume
    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project"),
    )

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            client,
            controller_store,
            task.id,
            ReportTaskRequest(summary="finished"),
        )

    assert error.value.status_code == 409
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value


@requires_docker
def test_an_agent_written_preview_proposal_cannot_move_the_task(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project"),
    )

    # The agent writes an untrusted preview proposal that claims the task is
    # finished and accepted, and commits nothing.
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "mkdir -p /project/.agent\n"
            "printf 'status: accepted\\nhead_commit: deadbeef\\n' "
            "> /project/.agent/preview.yaml\n"
        ),
    )

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            client,
            controller_store,
            task.id,
            ReportTaskRequest(summary="accepted"),
        )

    assert error.value.status_code == 409
    assert error.value.detail == (
        f"Task branch '{task.branch}' has no commit beyond {task.base_commit}"
    )
    row = controller_store.task(task.id)
    assert row["status"] == TaskStatus.OPEN.value
    assert row["head_commit"] is None


@requires_docker
def test_a_dirty_tree_names_its_paths_and_is_never_auto_committed(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project"),
    )
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "cd /project\n"
            "printf 'contact' > contact.astro\n"
            "git add -A\n"
            'git commit -q -m "add contact page"\n'
            "printf 'edited' > index.html\n"
        ),
    )

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            client,
            controller_store,
            task.id,
            ReportTaskRequest(),
        )

    assert error.value.status_code == 409
    assert "index.html" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value
    # The controller did not commit on the agent's behalf.
    assert (
        _run(client, volume.name, "cd /project && git log --format=%s | head -1")
        == "add contact page"
    )
    assert "index.html" in _run(
        client, volume.name, "cd /project && git status --porcelain"
    )


# --- Phase 4: accept and reject against a real git repository ---------------


def _review(client: Any, controller_store: ControllerStore, volume_name: str) -> Any:
    """Starts a task, commits on its branch, and drives it to review."""
    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project", title="Add a contact page"),
    )
    _run(
        client,
        volume_name,
        (
            "set -eu\n"
            "cd /project\n"
            "printf 'contact' > contact.astro\n"
            "git add -A\n"
            'git commit -q -m "add contact page"\n'
        ),
    )
    report_task_complete(client, controller_store, task.id, ReportTaskRequest())
    for target in (TaskStatus.PREVIEWING, TaskStatus.REVIEW):
        assert transition_task(controller_store, task_id=task.id, to_status=target)
    return get_task(controller_store, task.id)


def _branches(client: Any, volume_name: str) -> list[str]:
    listed = _run(
        client,
        volume_name,
        "cd /project && git for-each-ref --format='%(refname:short)' refs/heads/",
    )
    return [line.strip() for line in listed.splitlines() if line.strip()]


def _head(client: Any, volume_name: str, ref: str) -> str:
    return _run(client, volume_name, f'cd /project && git rev-parse --verify "{ref}"')


@requires_docker
def test_accept_fast_forwards_the_sandbox_branch_and_deletes_the_task_branch(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    reviewed_head = task.head_commit

    accepted = accept_task(client, controller_store, task.id)

    assert accepted.status is TaskStatus.ACCEPTED
    assert accepted.settled_at is not None
    assert _head(client, volume.name, "refs/heads/main") == reviewed_head
    assert _branches(client, volume.name) == ["main"]
    assert _run(client, volume.name, "cd /project && git branch --show-current") == (
        "main"
    )
    assert _run(client, volume.name, "cd /project && cat contact.astro") == "contact"
    # A fast-forward keeps every commit: no merge commit was created.
    assert _run(client, volume.name, "cd /project && git rev-list --count main") == "2"


@requires_docker
def test_accept_is_idempotent_against_a_real_repository(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    first = accept_task(client, controller_store, task.id)

    second = accept_task(client, controller_store, task.id)

    assert second.settled_at == first.settled_at
    assert second.status is TaskStatus.ACCEPTED
    assert _branches(client, volume.name) == ["main"]


@requires_docker
def test_a_diverged_sandbox_branch_returns_409_and_loses_no_commit(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)

    # Somebody commits on the sandbox branch after the task was cut, so the
    # task branch can no longer fast-forward into it.
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "cd /project\n"
            "git switch --quiet main\n"
            "printf 'about' > about.astro\n"
            "git add -A\n"
            'git commit -q -m "add about page"\n'
            f'git switch --quiet "{task.branch}"\n'
        ),
    )
    main_head = _head(client, volume.name, "refs/heads/main")

    with pytest.raises(TaskOperationError) as error:
        accept_task(client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "diverged" in error.value.detail
    assert "fast-forward merge is not possible" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value
    # Neither side moved, and neither side's commit is gone.
    assert _head(client, volume.name, "refs/heads/main") == main_head
    assert _head(client, volume.name, f"refs/heads/{task.branch}") == task.head_commit
    assert sorted(_branches(client, volume.name)) == sorted(["main", task.branch])
    assert "add about page" in _run(
        client, volume.name, "cd /project && git log --format=%s main"
    )
    assert "add contact page" in _run(
        client, volume.name, f'cd /project && git log --format=%s "{task.branch}"'
    )


@requires_docker
def test_accept_refuses_a_task_branch_that_moved_after_review(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "cd /project\n"
            "printf 'more' > extra.astro\n"
            "git add -A\n"
            'git commit -q -m "unreviewed work"\n'
        ),
    )

    with pytest.raises(TaskOperationError) as error:
        accept_task(client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "since review" in error.value.detail
    assert _head(client, volume.name, "refs/heads/main") != task.head_commit
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value


@requires_docker
def test_accept_refuses_an_uncommitted_worktree_and_keeps_it(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    _run(client, volume.name, "printf 'work in progress' > /project/index.html")

    with pytest.raises(TaskOperationError) as error:
        accept_task(client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "index.html" in error.value.detail
    # The uncommitted work is still there, untouched.
    assert _run(client, volume.name, "cd /project && cat index.html") == (
        "work in progress"
    )
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value


@requires_docker
def test_accept_settles_onto_a_sandbox_branch_that_is_not_main(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    # A sandbox imported from a host repository keeps that repository's branch.
    client, volume = sandbox_volume
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "cd /project\n"
            "git init -q -b master\n"
            'git config user.name "orchestrator"\n'
            'git config user.email "orchestrator@localhost"\n'
            "git add -A\n"
            'git commit -q -m "host history"\n'
        ),
    )
    task = _review(client, controller_store, volume.name)

    assert controller_store.task(task.id)["base_branch"] == "master"
    accepted = accept_task(client, controller_store, task.id)

    assert accepted.status is TaskStatus.ACCEPTED
    assert _head(client, volume.name, "refs/heads/master") == task.head_commit
    assert _branches(client, volume.name) == ["master"]


@requires_docker
def test_reject_leaves_the_sandbox_branch_untouched(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    main_head = _head(client, volume.name, "refs/heads/main")

    rejected = reject_task(client, controller_store, task.id)

    assert rejected.status is TaskStatus.REJECTED
    assert rejected.settled_at is not None
    assert _head(client, volume.name, "refs/heads/main") == main_head
    assert _branches(client, volume.name) == ["main"]
    assert _run(client, volume.name, "cd /project && git branch --show-current") == (
        "main"
    )
    # The rejected file never reached the sandbox branch.
    assert (
        _run(
            client,
            volume.name,
            "cd /project && ls contact.astro 2>/dev/null || echo gone",
        )
        == "gone"
    )


@requires_docker
def test_reject_frees_a_sandbox_whose_agent_never_committed(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project"),
    )
    # The agent leaves uncommitted work and never reports. Without open ->
    # rejected the sandbox could never open another task.
    _run(client, volume.name, "printf 'half done' > /project/draft.astro")

    rejected = reject_task(client, controller_store, task.id)

    assert rejected.status is TaskStatus.REJECTED
    assert _branches(client, volume.name) == ["main"]
    # The agent's uncommitted file is preserved, not discarded.
    assert _run(client, volume.name, "cd /project && cat draft.astro") == "half done"

    second = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project"),
    )
    assert second.id != task.id


@requires_docker
def test_a_task_branch_that_was_never_created_rejects_cleanly(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = start_task(
        client,
        controller_store,
        StartTaskRequest(project_name="Sample Project"),
    )
    _run(
        client,
        volume.name,
        (
            "set -eu\n"
            "cd /project\n"
            "git switch --quiet main\n"
            f'git branch -D "{task.branch}"\n'
        ),
    )

    rejected = reject_task(client, controller_store, task.id)

    assert rejected.status is TaskStatus.REJECTED
    assert _branches(client, volume.name) == ["main"]


@requires_docker
def test_reject_is_idempotent_against_a_real_repository(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    first = reject_task(client, controller_store, task.id)

    second = reject_task(client, controller_store, task.id)

    assert second.settled_at == first.settled_at
    assert _branches(client, volume.name) == ["main"]


@requires_docker
def test_reject_refuses_rather_than_overwrite_uncommitted_work(
    sandbox_volume: Any,
    controller_store: ControllerStore,
) -> None:
    client, volume = sandbox_volume
    task = _review(client, controller_store, volume.name)
    # The task branch changed contact.astro; an uncommitted edit to the same
    # path cannot survive a switch back, so git refuses and so do we.
    _run(client, volume.name, "printf 'uncommitted' > /project/contact.astro")

    with pytest.raises(TaskOperationError) as error:
        reject_task(client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "contact.astro" in error.value.detail
    assert _run(client, volume.name, "cd /project && cat contact.astro") == (
        "uncommitted"
    )
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value
