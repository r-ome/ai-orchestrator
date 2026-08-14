import base64
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import APIError

from app.controller.store import ControllerStore
from app.dirty_state import DirtyEntry, serialize_snapshot
from app.tasks.models import (
    TASK_TRANSITIONS,
    ReportTaskRequest,
    StartTaskRequest,
    TaskStatus,
    source_statuses,
)
from app.tasks.service import (
    TaskOperationError,
    accept_task,
    get_task,
    open_task_for_sandbox,
    reject_task,
    report_task_complete,
    start_task,
    transition_task,
)


BASE_COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
VOLUME_NAME = "orchestrator-project-sample"


class _StubContainers:
    """Answers the four git scripts app.tasks.service runs, and records them all."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.report_output = b""
        self.accept_output = b"result merged\nbase " + NEXT_COMMIT.encode() + b"\n"
        self.reject_output = b"result deleted\nbase " + BASE_COMMIT.encode() + b"\n"
        self.base_branch = "main"
        self.branch_error: Exception | None = None
        # What git already called dirty when the branch was cut. A sandbox
        # copied from a real repository arrives with untracked files.
        self.branch_dirty: tuple[str, ...] = ()
        self.branch_snapshot: tuple[str, ...] = ()

    def run(self, **kwargs: Any) -> bytes:
        script = kwargs["command"][0]
        self.scripts.append(script)
        if "git switch -c" in script:
            if self.branch_error is not None:
                raise self.branch_error
            lines = [f"base-branch {self.base_branch}"]
            if self.branch_snapshot:
                lines.append("snapshot-version 1")
                lines.extend(self.branch_snapshot)
            lines += [f"dirty {entry}" for entry in self.branch_dirty]
            lines.append(BASE_COMMIT)
            return ("\n".join(lines) + "\n").encode()
        if "result branch-moved" in script:
            return self.accept_output
        if "result switch-failed" in script:
            return self.reject_output
        if "git status --porcelain" in script:
            return self.report_output
        raise AssertionError(f"unexpected git script: {script}")


class _StubDockerClient:
    def __init__(self) -> None:
        self.containers = _StubContainers()


def _report_output(head: str, branch: str, dirty: tuple[str, ...] = ()) -> bytes:
    lines = [f"head {head}", f"branch {branch}"]
    lines.extend(f"dirty {entry}" for entry in dirty)
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def controller_store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    return store


@pytest.fixture
def docker_client(monkeypatch: pytest.MonkeyPatch) -> _StubDockerClient:
    project = SimpleNamespace(
        sandbox_id="sandbox-1",
        name="Sample Project",
        source_path="/projects/sample",
        volume_name=VOLUME_NAME,
        created_at="2026-01-01T00:00:00Z",
        ready=True,
    )
    monkeypatch.setattr(
        "app.tasks.service.inspect_registered_project",
        lambda *_: project,
    )
    monkeypatch.setattr(
        "app.tasks.service.ensure_git_baseline",
        lambda *_: BASE_COMMIT,
    )
    return _StubDockerClient()


def _start(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    title: str = "",
) -> Any:
    return start_task(
        docker_client,
        controller_store,
        StartTaskRequest(project_name="Sample Project", title=title),
    )


def test_starting_a_task_creates_a_branch_from_the_current_head(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store, title="Add a contact page")

    assert task.status is TaskStatus.OPEN
    assert task.branch == f"task/{task.id}"
    assert task.base_commit == BASE_COMMIT
    assert task.head_commit is None
    assert task.title == "Add a contact page"

    branch_script = docker_client.containers.scripts[-1]
    assert f'git switch -c "task/{task.id}"' in branch_script
    # The branch is only cut when HEAD is still where the row says it was.
    assert f'if [ "$head" != "{BASE_COMMIT}" ]' in branch_script


def test_task_row_precedes_git_baseline_and_opens_with_verified_base(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def baseline(*_: Any) -> str:
        rows = controller_store.tasks_for_sandbox("sandbox-1")
        assert len(rows) == 1
        assert rows[0]["status"] == TaskStatus.PREPARING.value
        assert rows[0]["base_commit"] == ""
        return BASE_COMMIT

    monkeypatch.setattr("app.tasks.service.ensure_git_baseline", baseline)

    task = _start(docker_client, controller_store)

    assert task.status is TaskStatus.OPEN
    assert task.base_commit == BASE_COMMIT
    assert task.base_branch == "main"


def test_baseline_failure_leaves_failed_row_and_allows_retry(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def baseline(*_: Any) -> str:
        nonlocal calls
        calls += 1
        preparing = [
            row
            for row in controller_store.tasks_for_sandbox("sandbox-1")
            if row["status"] == TaskStatus.PREPARING.value
        ]
        assert len(preparing) == 1
        assert preparing[0]["base_commit"] == ""
        if calls == 1:
            raise APIError("baseline failed")
        return BASE_COMMIT

    monkeypatch.setattr("app.tasks.service.ensure_git_baseline", baseline)

    with pytest.raises(APIError, match="baseline failed"):
        _start(docker_client, controller_store)

    failed = controller_store.tasks_for_sandbox("sandbox-1")
    assert len(failed) == 1
    assert failed[0]["status"] == TaskStatus.FAILED.value
    assert failed[0]["base_commit"] == ""

    retry = _start(docker_client, controller_store)

    assert retry.status is TaskStatus.OPEN
    assert calls == 2


def test_a_second_open_task_on_one_sandbox_is_rejected(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    first = _start(docker_client, controller_store)
    scripts_after_first = len(docker_client.containers.scripts)

    with pytest.raises(TaskOperationError) as error:
        _start(docker_client, controller_store)

    assert error.value.status_code == 409
    assert "already has an open task" in error.value.detail
    # The losing caller must not have touched git.
    assert len(docker_client.containers.scripts) == scripts_after_first
    assert [row["id"] for row in controller_store.tasks_for_sandbox("sandbox-1")] == [
        first.id
    ]


def test_a_settled_task_frees_the_sandbox_for_the_next_one(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    first = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(
        NEXT_COMMIT, first.branch
    )
    report_task_complete(
        docker_client,
        controller_store,
        first.id,
        ReportTaskRequest(),
    )
    for target in (
        TaskStatus.PREVIEWING,
        TaskStatus.REVIEW,
        TaskStatus.ACCEPTED,
    ):
        assert transition_task(controller_store, task_id=first.id, to_status=target)

    second = _start(docker_client, controller_store)

    assert second.id != first.id
    assert open_task_for_sandbox(controller_store, "sandbox-1").id == second.id


def test_a_report_with_an_unchanged_head_does_not_advance_status(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(BASE_COMMIT, task.branch)

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(summary="all done, promise"),
        )

    assert error.value.status_code == 409
    assert "no commit beyond" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value
    assert controller_store.task(task.id)["head_commit"] is None


def test_a_dirty_tree_returns_409_naming_the_dirty_paths(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(
        NEXT_COMMIT,
        task.branch,
        dirty=(" M src/pages/index.astro", "?? src/pages/contact.astro"),
    )

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(),
        )

    assert error.value.status_code == 409
    assert "src/pages/index.astro" in error.value.detail
    assert "src/pages/contact.astro" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value


def test_a_report_from_another_branch_is_rejected(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, "main")

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(),
        )

    assert error.value.status_code == 409
    assert "not the task branch" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value


def test_a_verified_commit_moves_the_task_to_reported(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)

    reported = report_task_complete(
        docker_client,
        controller_store,
        task.id,
        ReportTaskRequest(),
    )

    assert reported.status is TaskStatus.REPORTED
    assert reported.head_commit == NEXT_COMMIT
    # The head came from rev-parse against the ref, not from the agent.
    assert f'git rev-parse --verify "refs/heads/{task.branch}"' in (
        docker_client.containers.scripts[-1]
    )


def test_a_second_report_cannot_advance_an_already_reported_task(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    report_task_complete(docker_client, controller_store, task.id, ReportTaskRequest())

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(),
        )

    assert error.value.status_code == 409
    assert controller_store.task(task.id)["status"] == TaskStatus.REPORTED.value


def test_a_file_the_agent_writes_cannot_move_a_task_status(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    # An agent that writes a preview proposal claiming the work is accepted
    # gets exactly one thing: an untracked path in the dirty list.
    docker_client.containers.report_output = _report_output(
        BASE_COMMIT,
        task.branch,
        dirty=("?? .agent/preview.yaml",),
    )

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(summary="status: accepted"),
        )

    assert error.value.status_code == 409
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value
    # No git script the controller runs opens a file in the sandbox.
    for script in docker_client.containers.scripts:
        assert ".agent" not in script
        assert "preview.yaml" not in script
        assert "cat " not in script


def test_illegal_transitions_change_nothing(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)

    # rejected is reachable from open; reported is the other legal exit.
    for target in (
        TaskStatus.PREVIEWING,
        TaskStatus.REVIEW,
        TaskStatus.ACCEPTED,
        TaskStatus.FAILED,
    ):
        assert not transition_task(
            controller_store,
            task_id=task.id,
            to_status=target,
        )
        assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value


def test_settling_a_task_stamps_settled_at(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    report_task_complete(docker_client, controller_store, task.id, ReportTaskRequest())
    transition_task(
        controller_store, task_id=task.id, to_status=TaskStatus.PREVIEWING
    )
    transition_task(controller_store, task_id=task.id, to_status=TaskStatus.REVIEW)

    assert controller_store.task(task.id)["settled_at"] is None
    transition_task(controller_store, task_id=task.id, to_status=TaskStatus.REJECTED)
    assert controller_store.task(task.id)["settled_at"] is not None


def test_only_reported_tasks_can_reopen_for_controller_directed_repair() -> None:
    assert source_statuses(TaskStatus.OPEN) == frozenset({TaskStatus.REPORTED})
    assert source_statuses(TaskStatus.REPORTED) == frozenset({TaskStatus.OPEN})
    for terminal in (TaskStatus.ACCEPTED, TaskStatus.REJECTED, TaskStatus.FAILED):
        assert TASK_TRANSITIONS[terminal] == frozenset()


def test_a_failed_branch_creation_leaves_no_task_and_frees_the_slot(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    docker_client.containers.branch_error = APIError("branch creation failed")

    with pytest.raises(APIError):
        _start(docker_client, controller_store)

    assert controller_store.tasks_for_sandbox("sandbox-1") == []

    docker_client.containers.branch_error = None
    task = _start(docker_client, controller_store)
    assert task.status is TaskStatus.OPEN


def test_an_unknown_task_id_is_a_404(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            "../../etc/passwd",
            ReportTaskRequest(),
        )

    assert error.value.status_code == 404
    assert docker_client.containers.scripts == []


def test_initial_migration_creates_task_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "controller.sqlite3"
    store = ControllerStore(database_path)
    store.initialize()
    store.initialize()
    store.register_sandbox(
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        source_path="/projects/sample",
        volume_name="orchestrator-project-sample-1",
        status="ready",
        created_at="2026-01-01T00:00:00Z",
    )

    store.create_task(
        task_id="c" * 32,
        sandbox_id="sandbox-1",
        agent_run_id=None,
        branch=f"task/{'c' * 32}",
        base_branch="main",
        base_commit=BASE_COMMIT,
        title="",
        status=TaskStatus.OPEN.value,
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(
            task_id="d" * 32,
            sandbox_id="sandbox-1",
            agent_run_id=None,
            branch=f"task/{'d' * 32}",
            base_branch="main",
            base_commit=BASE_COMMIT,
            title="",
            status=TaskStatus.OPEN.value,
        )

    connection = sqlite3.connect(database_path)
    try:
        versions = [
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        ]
        sandbox_rows = connection.execute("SELECT count(*) FROM sandboxes").fetchone()
    finally:
        connection.close()

    assert versions == [1, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]
    assert sandbox_rows[0] == 1


# --- Phase 4: accept and reject -------------------------------------------


def _to_review(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> Any:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    report_task_complete(docker_client, controller_store, task.id, ReportTaskRequest())
    transition_task(controller_store, task_id=task.id, to_status=TaskStatus.PREVIEWING)
    transition_task(controller_store, task_id=task.id, to_status=TaskStatus.REVIEW)
    return _task_model(controller_store, task.id)


def _task_model(controller_store: ControllerStore, task_id: str) -> Any:
    return get_task(controller_store, task_id)


def _settle_script(docker_client: _StubDockerClient) -> str:
    return docker_client.containers.scripts[-1]


def test_accept_fast_forwards_the_sandbox_branch_and_removes_the_task_branch(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)

    accepted = accept_task(docker_client, controller_store, task.id)

    assert accepted.status is TaskStatus.ACCEPTED
    assert accepted.settled_at is not None
    script = _settle_script(docker_client)
    assert f'git merge --ff-only --quiet "{task.branch}"' in script
    assert f'git branch -d "{task.branch}"' in script
    assert 'git switch --quiet "main"' in script
    # The reviewed commit is what the merge is checked against.
    assert f'if [ "$task_head" != "{NEXT_COMMIT}" ]' in script


def test_accept_on_a_diverged_sandbox_branch_returns_409_and_stays_in_review(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    docker_client.containers.accept_output = (
        b"result diverged\n"
        b"base " + BASE_COMMIT.encode() + b"\n"
        b"task " + NEXT_COMMIT.encode() + b"\n"
        b"counts 2 3\n"
    )

    with pytest.raises(TaskOperationError) as error:
        accept_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "diverged" in error.value.detail
    assert "2 commit(s) only on 'main'" in error.value.detail
    assert "3 only on the task branch" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value
    assert controller_store.task(task.id)["settled_at"] is None


def test_accept_refuses_a_task_branch_that_moved_since_review(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    docker_client.containers.accept_output = b"result branch-moved\ntask " + (
        b"c" * 40
    ) + b"\n"

    with pytest.raises(TaskOperationError) as error:
        accept_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "moved to " + "c" * 40 in error.value.detail
    assert NEXT_COMMIT in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value


def test_accept_refuses_an_uncommitted_worktree(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    docker_client.containers.accept_output = (
        b"result dirty\ndetail  M src/pages/index.astro\n"
    )

    with pytest.raises(TaskOperationError) as error:
        accept_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "src/pages/index.astro" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value


def test_accept_refuses_when_the_task_branch_is_gone_and_unmerged(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    docker_client.containers.accept_output = b"result missing-task-branch\n"

    with pytest.raises(TaskOperationError) as error:
        accept_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "does not contain" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value


def test_accept_is_idempotent(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    first = accept_task(docker_client, controller_store, task.id)
    scripts_after_first = len(docker_client.containers.scripts)

    second = accept_task(docker_client, controller_store, task.id)

    assert second.status is TaskStatus.ACCEPTED
    assert second.settled_at == first.settled_at
    # The second call is a read: it must not run git again.
    assert len(docker_client.containers.scripts) == scripts_after_first


def test_a_retried_accept_completes_after_the_status_write_was_lost(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Git merged, then the guarded UPDATE lost a race. The caller is told the
    # truth, and a retry against an already-merged sandbox still settles.
    task = _to_review(docker_client, controller_store)
    monkeypatch.setattr(
        "app.tasks.service.transition_task",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(TaskOperationError) as error:
        accept_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "merged into 'main'" in error.value.detail
    assert "no commit was lost" in error.value.detail

    monkeypatch.undo()
    docker_client.containers.accept_output = (
        b"result already-merged\nbase " + NEXT_COMMIT.encode() + b"\n"
    )
    retried = accept_task(docker_client, controller_store, task.id)
    assert retried.status is TaskStatus.ACCEPTED


def test_accept_only_runs_on_a_reviewed_task(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)

    with pytest.raises(TaskOperationError) as error:
        accept_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "not in review" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.OPEN.value


def test_accept_stops_the_task_preview_and_keeps_the_dependency_volume(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _to_review(docker_client, controller_store)
    stopped: list[dict[str, Any]] = []
    monkeypatch.setattr(
        controller_store,
        "active_preview",
        lambda sandbox_id: {"id": "preview-1", "task_id": task.id},
    )
    monkeypatch.setattr(
        "app.previews.service.stop_preview",
        lambda *args, **kwargs: stopped.append(kwargs) or SimpleNamespace(),
    )

    accept_task(docker_client, controller_store, task.id)

    assert stopped == [{"remove_data_volumes": True, "status": "stopped"}]
    # remove_data_volumes never reaches the dependency volume: _remove_resources
    # skips every volume labelled persistent, and _dependency_volume sets that
    # label. The behaviour is asserted end to end in the docker preview tests.


def test_a_preview_that_belongs_to_another_task_is_left_running(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _to_review(docker_client, controller_store)
    monkeypatch.setattr(
        controller_store,
        "active_preview",
        lambda sandbox_id: {"id": "preview-1", "task_id": None},
    )
    monkeypatch.setattr(
        "app.previews.service.stop_preview",
        lambda *args, **kwargs: pytest.fail("stopped a preview it does not own"),
    )

    assert accept_task(docker_client, controller_store, task.id).status is (
        TaskStatus.ACCEPTED
    )


def test_a_failed_preview_stop_does_not_unsettle_an_accepted_task(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _to_review(docker_client, controller_store)
    monkeypatch.setattr(
        controller_store,
        "active_preview",
        lambda sandbox_id: {"id": "preview-1", "task_id": task.id},
    )

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise APIError("docker is unhappy")

    monkeypatch.setattr("app.previews.service.stop_preview", _explode)

    accepted = accept_task(docker_client, controller_store, task.id)

    assert accepted.status is TaskStatus.ACCEPTED
    kinds = [
        event["kind"]
        for event in controller_store.events_for_run(task.id)
    ]
    assert "task.preview_stop_failed" in kinds


def test_reject_works_on_a_task_that_never_left_open(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)

    rejected = reject_task(docker_client, controller_store, task.id)

    assert rejected.status is TaskStatus.REJECTED
    assert rejected.settled_at is not None
    # The slot is free again, which is the whole reason open -> rejected exists.
    assert open_task_for_sandbox(controller_store, "sandbox-1") is None
    assert _start(docker_client, controller_store).id != task.id


def test_reject_works_on_a_reported_task(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    report_task_complete(
        docker_client,
        controller_store,
        task.id,
        ReportTaskRequest(),
    )

    rejected = reject_task(docker_client, controller_store, task.id)

    assert rejected.status is TaskStatus.REJECTED
    assert open_task_for_sandbox(controller_store, "sandbox-1") is None


def test_reject_deletes_only_the_task_branch(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)

    reject_task(docker_client, controller_store, task.id)

    script = _settle_script(docker_client)
    assert f'git branch -D "{task.branch}"' in script
    # Nothing in the reject script can move or delete the sandbox branch.
    assert "branch -D \"main\"" not in script
    assert "reset" not in script
    assert "merge" not in script


def test_a_missing_task_branch_is_a_completed_reject(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    docker_client.containers.reject_output = b"result missing-task-branch\n"

    assert reject_task(docker_client, controller_store, task.id).status is (
        TaskStatus.REJECTED
    )


def test_reject_is_idempotent(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _start(docker_client, controller_store)
    first = reject_task(docker_client, controller_store, task.id)
    scripts_after_first = len(docker_client.containers.scripts)

    second = reject_task(docker_client, controller_store, task.id)

    assert second.settled_at == first.settled_at
    assert len(docker_client.containers.scripts) == scripts_after_first


def test_reject_refuses_to_overwrite_uncommitted_work(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    docker_client.containers.reject_output = (
        b"result switch-failed\n"
        b"detail error: Your local changes to the following files would be "
        b"overwritten by checkout:\n"
        b"detail \tsrc/pages/index.astro\n"
    )

    with pytest.raises(TaskOperationError) as error:
        reject_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert "src/pages/index.astro" in error.value.detail
    assert controller_store.task(task.id)["status"] == TaskStatus.REVIEW.value


def test_an_accepted_task_cannot_be_rejected(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    accept_task(docker_client, controller_store, task.id)

    with pytest.raises(TaskOperationError) as error:
        reject_task(docker_client, controller_store, task.id)

    assert error.value.status_code == 409
    assert controller_store.task(task.id)["status"] == TaskStatus.ACCEPTED.value


def test_the_base_branch_comes_from_the_task_row_not_from_a_default(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    # A sandbox imported from a host repository keeps that repository's branch.
    docker_client.containers.base_branch = "master"
    task = _to_review(docker_client, controller_store)

    assert controller_store.task(task.id)["base_branch"] == "master"
    accept_task(docker_client, controller_store, task.id)

    script = _settle_script(docker_client)
    assert 'git switch --quiet "master"' in script
    assert '"refs/heads/master"' in script
    assert "main" not in script


def test_no_settlement_script_can_rewrite_history_or_force_a_ref(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    accept_task(docker_client, controller_store, task.id)
    second = _to_review(docker_client, controller_store)
    reject_task(docker_client, controller_store, second.id)

    forbidden = (
        "reset --hard",
        "rebase",
        "push",
        "fetch",
        "clone",
        "--force",
        "-f ",
        "commit",
        "update-ref",
        "reflog expire",
        "gc --prune",
        "filter-branch",
    )
    for script in docker_client.containers.scripts:
        for token in forbidden:
            assert token not in script, f"{token!r} in {script!r}"
    # The one deletion that exists is of a task branch, never of a sandbox one.
    for script in docker_client.containers.scripts:
        for line in script.splitlines():
            if "branch -D" in line or "branch -d" in line:
                assert "task/" in line


def test_a_settlement_never_reads_a_file_the_agent_wrote(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    task = _to_review(docker_client, controller_store)
    accept_task(docker_client, controller_store, task.id)

    for script in docker_client.containers.scripts:
        assert "cat " not in script
        assert ".agent" not in script
        assert "preview.yaml" not in script


def test_a_branch_name_that_would_break_out_of_the_script_is_refused(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    # Git allows a double quote in a ref name; the settlement scripts quote
    # the branch, so such a name is refused at start rather than escaped.
    docker_client.containers.base_branch = 'main"; rm -rf /project; echo "'

    with pytest.raises(TaskOperationError) as error:
        _start(docker_client, controller_store)

    assert error.value.status_code == 409
    assert "unusable base branch" in error.value.detail
    # The refused task frees its slot again.
    assert controller_store.tasks_for_sandbox("sandbox-1") == []


def test_every_open_status_can_reach_a_terminal_one() -> None:
    """A status whose exits are all unavailable holds the sandbox's single
    task slot forever. Walk the graph rather than trusting the table by eye."""
    from app.tasks.models import (
        OPEN_TASK_STATUSES,
        TASK_TRANSITIONS,
        TERMINAL_TASK_STATUSES,
    )

    for start in OPEN_TASK_STATUSES:
        seen: set[TaskStatus] = set()
        frontier = [start]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(TASK_TRANSITIONS[current])
        assert seen & TERMINAL_TASK_STATUSES, f"'{start.value}' cannot settle"


def test_a_reported_task_can_reach_review_without_a_preview() -> None:
    """The non-preview path a delegated run takes. A unit with nothing to
    preview must still be acceptable once its branch verifies."""
    from app.tasks.models import TASK_TRANSITIONS, TaskStatus

    assert TaskStatus.REVIEW in TASK_TRANSITIONS[TaskStatus.REPORTED]
    assert TaskStatus.PREVIEWING in TASK_TRANSITIONS[TaskStatus.REPORTED]


def test_a_reported_task_can_be_rejected() -> None:
    from app.tasks.models import TASK_TRANSITIONS, TaskStatus

    assert TaskStatus.REJECTED in TASK_TRANSITIONS[TaskStatus.REPORTED]


def test_open_task_statuses_match_the_partial_index(tmp_path: Path) -> None:
    """The index is what enforces one open task; this set is what the API
    reads back. If they drift, two tasks can be open at once."""
    import re
    import sqlite3 as sql

    from app.controller.store import ControllerStore
    from app.tasks.models import OPEN_TASK_STATUSES

    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    with sql.connect(store.database_path) as connection:
        sql_text = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'one_open_task_per_sandbox'"
        ).fetchone()[0]

    assert set(re.findall(r"'([a-z]+)'", sql_text)) == {
        status.value for status in OPEN_TASK_STATUSES
    }


# --- Headless coding turns -------------------------------------------------


def _turn(status: str = "succeeded", **overrides: Any):
    from app.agents.models import AgentProvider
    from app.tasks.runner import CodingTurnResult, ToolCall, TurnUsage

    fields: dict[str, Any] = {
        "provider": AgentProvider.CLAUDE,
        "model": "claude-sonnet-5",
        "status": status,
        "text": '{"changed": ["src/app.py"]}',
        "payload": {"changed": ["src/app.py"]},
        "usage": TurnUsage(input_tokens=11, output_tokens=22, cost_usd=0.05),
        "tool_calls": (ToolCall(name="Edit", failed=False),),
        "duration_ms": 1234,
        "exit_code": 0,
    }
    fields.update(overrides)
    return CodingTurnResult(**fields)


def _set_turn(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _run(*_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return result

    monkeypatch.setattr("app.tasks.runner.run_coding_turn", _run)
    return calls


def _settings() -> Any:
    from app.tasks.config import get_coding_turn_settings

    return get_coding_turn_settings()


def _open(docker_client: _StubDockerClient, controller_store: ControllerStore) -> Any:
    return start_task(
        docker_client,
        controller_store,
        StartTaskRequest(project_name="Sample Project", title="Add a thing"),
    )


def test_a_headless_turn_that_commits_reaches_reported(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    calls = _set_turn(monkeypatch, _turn())

    response = run_task(
        docker_client,
        controller_store,
        _settings(),
        task.id,
        RunTaskRequest(prompt="do the thing"),
    )

    assert response.committed is True
    assert response.task.status is TaskStatus.REPORTED
    assert response.task.head_commit == NEXT_COMMIT
    # The turn runs on the branch start_task already switched to.
    assert calls[0]["volume_name"] == VOLUME_NAME
    assert calls[0]["prompt"].startswith("do the thing\n\n## Completion contract")
    assert "Commit all intended changes" in calls[0]["prompt"]
    assert '"notes_for_downstream"' in calls[0]["prompt"]


def test_the_run_records_cost_model_and_tool_outcomes(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())

    response = run_task(
        docker_client, controller_store, _settings(), task.id,
        RunTaskRequest(prompt="p"),
    )

    assert response.usage.cost_usd == 0.05
    assert response.usage.input_tokens == 11
    assert response.duration_ms == 1234
    assert response.exit_code == 0
    assert response.model == "claude-sonnet-5"
    assert response.tool_calls == 1
    assert response.failed_tool_calls == 0
    assert response.result == {"changed": ["src/app.py"]}


def test_a_turn_that_claims_success_without_committing_leaves_the_task_open(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn reports success; the branch carries nothing. The branch wins,
    and the task stays open so a retry is possible."""
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(BASE_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())

    response = run_task(
        docker_client, controller_store, _settings(), task.id,
        RunTaskRequest(prompt="p"),
    )

    assert response.turn_status == "succeeded"
    assert response.committed is False
    assert response.task.status is TaskStatus.OPEN
    assert "no commit beyond" in response.detail


def test_a_failed_turn_leaves_the_task_open_and_reports_why(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task

    task = _open(docker_client, controller_store)
    _set_turn(
        monkeypatch,
        _turn("tool_failure", error="All 2 tool calls failed", payload=None),
    )

    response = run_task(
        docker_client, controller_store, _settings(), task.id,
        RunTaskRequest(prompt="p"),
    )

    assert response.turn_status == "tool_failure"
    assert response.committed is False
    assert response.task.status is TaskStatus.OPEN
    assert response.turn_error == "All 2 tool calls failed"
    # Cost is recorded even for an attempt that produced nothing.
    assert response.usage.cost_usd == 0.05


def test_a_task_that_is_not_open_refuses_to_run(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())
    run_task(docker_client, controller_store, _settings(), task.id, RunTaskRequest(prompt="p"))

    with pytest.raises(TaskOperationError) as error:
        run_task(
            docker_client, controller_store, _settings(), task.id,
            RunTaskRequest(prompt="p"),
        )

    assert error.value.status_code == 409


def test_verify_moves_a_reported_task_to_review_without_a_preview(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-preview path: a unit with nothing to preview still becomes
    acceptable once its branch verifies."""
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task, verify_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())
    run_task(docker_client, controller_store, _settings(), task.id, RunTaskRequest(prompt="p"))

    verified = verify_task(controller_store, task.id)

    assert verified.status is TaskStatus.REVIEW


def test_a_verified_task_can_then_be_accepted(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task, verify_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())
    run_task(docker_client, controller_store, _settings(), task.id, RunTaskRequest(prompt="p"))
    verify_task(controller_store, task.id)

    accepted = accept_task(docker_client, controller_store, task.id)

    assert accepted.status is TaskStatus.ACCEPTED


def test_verify_refuses_a_task_that_has_not_reported(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    from app.tasks.service import verify_task

    task = _open(docker_client, controller_store)

    with pytest.raises(TaskOperationError) as error:
        verify_task(controller_store, task.id)

    assert error.value.status_code == 409


def test_verify_refuses_when_verification_did_not_pass(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import run_task, verify_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())
    run_task(docker_client, controller_store, _settings(), task.id, RunTaskRequest(prompt="p"))

    with pytest.raises(TaskOperationError) as error:
        verify_task(
            controller_store, task.id, verification_passed=False, detail="build failed"
        )

    assert error.value.status_code == 409
    assert "build failed" in error.value.detail
    assert get_task(controller_store, task.id).status is TaskStatus.REPORTED


def test_controller_can_reopen_reported_task_for_repair(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.models import RunTaskRequest
    from app.tasks.service import reopen_task_for_repair, run_task

    task = _open(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    _set_turn(monkeypatch, _turn())
    run_task(
        docker_client,
        controller_store,
        _settings(),
        task.id,
        RunTaskRequest(prompt="p"),
    )

    reopened = reopen_task_for_repair(controller_store, task.id)

    assert reopened.status is TaskStatus.OPEN
    assert reopened.head_commit == NEXT_COMMIT
    assert any(
        event["kind"] == "task.repair_started"
        for event in controller_store.events_for_run(task.id)
    )


def test_paths_already_dirty_when_the_branch_was_cut_do_not_fail_the_report(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    """A sandbox copied from a real repository carries untracked files.

    `.agent/`, `.claude/` and friends are not in .gitignore and are not the
    turn's work. Refusing on them made every task in such a sandbox impossible
    to settle no matter what the model committed.
    """
    docker_client.containers.branch_dirty = ("?? .agent/", "?? .claude/")
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(
        NEXT_COMMIT,
        task.branch,
        dirty=("?? .agent/", "?? .claude/"),
    )

    reported = report_task_complete(
        docker_client,
        controller_store,
        task.id,
        ReportTaskRequest(),
    )

    assert reported.status is TaskStatus.REPORTED
    assert reported.head_commit == NEXT_COMMIT


def test_work_the_turn_left_uncommitted_still_fails_the_report(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    """The baseline excuses its own paths and nothing else."""
    docker_client.containers.branch_dirty = ("?? .agent/",)
    task = _start(docker_client, controller_store)
    docker_client.containers.report_output = _report_output(
        NEXT_COMMIT,
        task.branch,
        dirty=("?? .agent/", " M src/pages/index.astro"),
    )

    with pytest.raises(TaskOperationError) as error:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(),
        )

    assert error.value.status_code == 409
    assert "src/pages/index.astro" in error.value.detail
    assert ".agent/" not in error.value.detail


def test_the_baseline_is_recorded_and_passed_to_the_accept_script(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    """Accept refuses a dirty worktree too, so it needs the same exemption."""
    docker_client.containers.branch_dirty = ("?? .agent/", "?? interview-prep/")
    task = _start(docker_client, controller_store)

    assert controller_store.task(task.id)["baseline_dirty_json"] == (
        '[".agent/", "interview-prep/"]'
    )

    docker_client.containers.report_output = _report_output(NEXT_COMMIT, task.branch)
    report_task_complete(
        docker_client,
        controller_store,
        task.id,
        ReportTaskRequest(),
    )
    for target in (TaskStatus.PREVIEWING, TaskStatus.REVIEW):
        assert transition_task(controller_store, task_id=task.id, to_status=target)

    accept_task(docker_client, controller_store, task.id)

    accept_script = docker_client.containers.scripts[-1]
    assert "baseline='.agent/\ninterview-prep/'" in accept_script


def test_the_first_task_records_a_fingerprinted_sandbox_dirty_baseline(
    docker_client: _StubDockerClient,
    controller_store: ControllerStore,
) -> None:
    position = DirtyEntry(
        path="interview-prep/Position.md",
        status="??",
        file_type="file",
        fingerprint="sha256:original",
    )
    status = base64.b64encode(position.status.encode()).decode()
    path = base64.b64encode(position.path.encode()).decode()
    docker_client.containers.branch_snapshot = (
        f"snapshot {status} file {position.fingerprint} {path}",
    )
    docker_client.containers.branch_dirty = ("?? interview-prep/Position.md",)

    _start(docker_client, controller_store)

    assert controller_store.sandbox("sandbox-1")["dirty_baseline_json"] == (
        serialize_snapshot([position])
    )
