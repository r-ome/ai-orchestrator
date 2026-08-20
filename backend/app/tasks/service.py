import json
import re
import shlex
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import uuid4

from docker.client import DockerClient

from app.containers.config import get_git_settings
from app.containers.git import run_git
from app.controller.store import (
    ControllerStore,
    OpenTaskExists,
    SandboxWriterAdmissionError,
)
from app.controller.store.task_status import (
    OPEN_TASK_STATUSES,
    TaskStatus,
    transition_task,
)
from app.platform.dirty_state import parse_snapshot, serialize_snapshot, snapshot_shell
from app.platform.errors import OperationError
from app.previews.service import stop_task_preview
from app.projects.models import ProjectRegistration
from app.projects.service import (
    ProjectOperationError,
    ensure_git_baseline,
    ensure_sandbox_registered,
    inspect_registered_project,
)
from app.tasks.config import CodingTurnSettings

if TYPE_CHECKING:  # pragma: no cover - types only
    from app.tasks.runner import CodingTurnResult
from app.tasks.models import (
    DEFAULT_BASE_BRANCH,
    ReportTaskRequest,
    RunTaskRequest,
    StartTaskRequest,
    Task,
    TaskRunResponse,
    TasksResponse,
    TurnUsageView,
)
from app.tasks.runner import run_coding_turn

TASK_BRANCH_PREFIX = "task/"
_TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class TaskOperationError(OperationError):
    """A task operation failed."""


def start_task(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    request: StartTaskRequest,
) -> Task:
    project, sandbox_id = _resolve_sandbox(
        docker_client,
        controller_store,
        request.project_name,
    )
    if not project.ready:
        raise TaskOperationError(
            409,
            f"Sandbox '{request.project_name}' is not ready",
        )
    task_id = uuid4().hex
    branch = f"{TASK_BRANCH_PREFIX}{task_id}"
    active_agent = controller_store.active_agent(sandbox_id)
    try:
        controller_store.create_task(
            task_id=task_id,
            sandbox_id=sandbox_id,
            agent_run_id=str(active_agent["id"]) if active_agent else None,
            branch=branch,
            base_branch="",
            base_commit="",
            title=request.title,
            status=TaskStatus.PREPARING.value,
        )
    except SandboxWriterAdmissionError as error:
        raise TaskOperationError(409, str(error)) from error
    except OpenTaskExists as error:
        raise TaskOperationError(
            409,
            f"Sandbox '{project.name}' already has an open task",
        ) from error

    # The preparing row covers baseline creation too. ensure_git_baseline can
    # initialize Git and create a commit, so it is sandbox mutation rather
    # than a harmless read.
    git_image = get_git_settings().git_image
    try:
        base_commit = ensure_git_baseline(
            docker_client,
            git_image,
            project.volume_name,
        )
        if not _COMMIT_PATTERN.match(base_commit):
            raise TaskOperationError(
                502,
                "Sandbox git baseline did not return a commit hash",
            )
        if not controller_store.sandbox_baseline_commit(sandbox_id):
            controller_store.set_sandbox_baseline_commit(
                sandbox_id=sandbox_id,
                baseline_commit=base_commit,
            )
    except Exception:
        transition_task(
            controller_store,
            task_id=task_id,
            to_status=TaskStatus.FAILED,
        )
        raise

    try:
        output = run_git(
            docker_client,
            image=git_image,
            volumes={project.volume_name: {"bind": "/project", "mode": "rw"}},
            script=_branch_script(branch, base_commit),
        )
        base_branch = _parse_base_branch(output)
        if not base_branch:
            raise TaskOperationError(
                502,
                "Sandbox git did not report the branch the task was cut from",
            )
        controller_store.set_task_baseline_dirty(
            task_id=task_id,
            paths=_parse_dirty(output),
        )
        dirty_snapshot = parse_snapshot(output)
        if dirty_snapshot is not None:
            controller_store.set_sandbox_dirty_baseline_if_missing(
                sandbox_id=sandbox_id,
                baseline_json=serialize_snapshot(dirty_snapshot),
            )
        # Checked here as well as at settlement, so a sandbox on a branch name
        # this code cannot handle refuses the task instead of opening one that
        # can never be accepted or rejected.
        _validated_branch(base_branch, task_id=task_id)
        if not controller_store.complete_task_preparation(
            task_id=task_id,
            base_branch=base_branch,
            base_commit=base_commit,
        ):
            raise TaskOperationError(
                409,
                f"Task '{task_id}' changed while it was being prepared",
            )
    except Exception:
        transition_task(
            controller_store,
            task_id=task_id,
            to_status=TaskStatus.FAILED,
        )
        # Keep the established branch-failure behavior. The failed status is
        # recorded before the existing compensating deletion frees the slot.
        controller_store.delete_task(task_id=task_id)
        raise

    return _task(controller_store, task_id)


def run_task(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: CodingTurnSettings,
    task_id: str,
    request: RunTaskRequest,
) -> TaskRunResponse:
    """Run one headless coding turn on an open task's branch.

    The turn works on the branch start_task already switched the sandbox to.
    What it says about itself is recorded; whether the task advances is decided
    by report_task_complete reading the branch, exactly as it is for a turn a
    human drove.

    A turn that fails leaves the task open. Nothing was committed, so a retry
    is legitimate and the caller decides between another attempt and rejecting.
    """
    task = _task(controller_store, _validated_task_id(task_id))
    if task.status is not TaskStatus.OPEN:
        raise TaskOperationError(
            409,
            f"Task '{task.id}' is not open (status '{task.status.value}')",
        )
    sandbox = controller_store.sandbox(task.sandbox_id)
    if sandbox is None:
        raise TaskOperationError(404, f"Sandbox '{task.sandbox_id}' is unknown")

    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.turn_started",
        payload={"provider": request.provider.value, "model": request.model or ""},
    )
    result = run_coding_turn(
        docker_client,
        settings,
        task_id=task.id,
        volume_name=str(sandbox["volume_name"]),
        provider=request.provider,
        prompt=_task_prompt(request.prompt),
        model=request.model,
        controller_store=controller_store,
        sandbox_id=task.sandbox_id,
    )
    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.turn_finished",
        payload={
            "status": result.status,
            "model": result.model,
            "cost_usd": result.usage.cost_usd,
            "duration_ms": result.duration_ms,
        },
    )

    if not result.succeeded:
        return _run_response(
            controller_store,
            task.id,
            result,
            committed=False,
            detail=result.error or "",
        )

    # The turn says it is done. The branch decides whether it is.
    try:
        report_task_complete(
            docker_client,
            controller_store,
            task.id,
            ReportTaskRequest(summary=result.text[:2000]),
        )
    except TaskOperationError as error:
        # A turn that ran cleanly but left nothing on the branch is an ordinary
        # outcome for an unattended run, not an exception the caller must catch.
        return _run_response(
            controller_store, task.id, result, committed=False, detail=error.detail
        )

    return _run_response(
        controller_store,
        task.id,
        result,
        committed=True,
        detail="branch verified",
    )


def verify_task(
    controller_store: ControllerStore,
    task_id: str,
    *,
    verification_passed: bool = True,
    detail: str = "",
) -> Task:
    """Move a reported task to review without a preview.

    The non-preview path. A delegated unit — a shared helper, a migration, a
    refactor — often has nothing to preview, and a mid-graph unit can leave the
    application temporarily unbuildable, so requiring a preview stack would
    make such work unacceptable rather than unverified. The git check already
    ran in report_task_complete; `verification_passed` carries the outcome of
    the item's configured commands once those run.
    """
    task = _task(controller_store, _validated_task_id(task_id))
    if task.status is not TaskStatus.REPORTED:
        raise TaskOperationError(
            409,
            f"Task '{task.id}' is not reported (status '{task.status.value}')",
        )
    if not verification_passed:
        raise TaskOperationError(
            409,
            f"Verification did not pass for task '{task.id}': {detail or 'no detail'}",
        )
    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.verified",
        payload={"detail": detail},
    )
    transition_task(
        controller_store,
        task_id=task.id,
        to_status=TaskStatus.REVIEW,
    )
    return _task(controller_store, task.id)


def reopen_task_for_repair(
    controller_store: ControllerStore,
    task_id: str,
) -> Task:
    """Return a reported task to open for one controller-directed repair."""
    task = _task(controller_store, _validated_task_id(task_id))
    if task.status is not TaskStatus.REPORTED:
        raise TaskOperationError(
            409,
            f"Task '{task.id}' is not reported (status '{task.status.value}')",
        )
    if not transition_task(
        controller_store,
        task_id=task.id,
        to_status=TaskStatus.OPEN,
    ):
        raise TaskOperationError(409, f"Task '{task.id}' could not reopen for repair")
    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.repair_started",
        payload={"previous_head": task.head_commit or ""},
    )
    return _task(controller_store, task.id)


def _run_response(
    controller_store: ControllerStore,
    task_id: str,
    result: "CodingTurnResult",
    *,
    committed: bool,
    detail: str,
) -> TaskRunResponse:
    failed = sum(1 for call in result.tool_calls if call.failed)
    return TaskRunResponse(
        task=_task(controller_store, task_id),
        turn_status=result.status,
        turn_error=result.error,
        committed=committed,
        detail=detail,
        model=result.model,
        usage=TurnUsageView(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_creation_tokens=result.usage.cache_creation_tokens,
            cost_usd=result.usage.cost_usd,
        ),
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        tool_calls=len(result.tool_calls),
        failed_tool_calls=failed,
        result=result.payload,
    )


def _task_prompt(prompt: str) -> str:
    """Add the task-layer completion contract to a caller's work prompt."""
    return f"""{prompt.rstrip()}

## Completion contract

Commit all intended changes to the current task branch with a clear message.
Do not switch branches. Do not push.

Return exactly one JSON object as the last content in your reply. Use this shape:

{{
  "changed": ["what changed"],
  "decisions": ["implementation decisions"],
  "interfaces": ["interfaces introduced or changed"],
  "change_kind": "interactive_ui | api_behavior | data_behavior | static_code",
  "acceptance_criteria": [
    {{
      "criterion": "observable result",
      "verification_kind": "behavior_test | static_check | manual_check",
      "verified": true,
      "evidence": "test evidence"
    }}
  ],
  "verification": {{
    "ran": ["commands you ran"],
    "outcome": "not_run",
    "detail": "optional detail"
  }},
  "notes_for_downstream": ["information later work items need"]
}}
"""


def list_tasks(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> TasksResponse:
    _, sandbox_id = _resolve_sandbox(docker_client, controller_store, project_name)
    tasks = [
        Task.model_validate(row)
        for row in controller_store.tasks_for_sandbox(sandbox_id)
    ]
    return TasksResponse(count=len(tasks), tasks=tasks)


def get_task(controller_store: ControllerStore, task_id: str) -> Task:
    return _task(controller_store, _validated_task_id(task_id))


def report_task_complete(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    task_id: str,
    request: ReportTaskRequest,
) -> Task:
    """Verifies an agent's completion claim against the task branch itself.

    The claim decides nothing. The controller runs git in a throwaway
    container, reads the branch head and the worktree state, and advances the
    task only when a real commit exists on the branch. No file inside the
    sandbox volume is read, so /workspace/.agent/preview.yaml and anything
    else the agent authored is inert here.
    """
    task = _task(controller_store, _validated_task_id(task_id))
    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.completion_claimed",
        payload={"summary": request.summary},
    )
    if task.status is not TaskStatus.OPEN:
        raise TaskOperationError(
            409,
            f"Task '{task.id}' is not open (status '{task.status.value}')",
        )

    sandbox = controller_store.sandbox(task.sandbox_id)
    if sandbox is None:
        raise TaskOperationError(404, f"Sandbox '{task.sandbox_id}' is unknown")
    volume_name = str(sandbox["volume_name"])

    git_image = get_git_settings().git_image
    output = run_git(
        docker_client,
        image=git_image,
        volumes={volume_name: {"bind": "/project", "mode": "rw"}},
        script=_report_script(task.branch),
    )
    head_commit, checked_out, dirty_paths = _parse_report(output)

    if checked_out != task.branch:
        raise TaskOperationError(
            409,
            (
                f"Sandbox is on branch '{checked_out or 'a detached HEAD'}', "
                f"not the task branch '{task.branch}'"
            ),
        )
    # Only paths the turn itself left behind count. Anything already dirty when
    # the branch was cut is the sandbox's own baggage, and failing on it would
    # make every run in an imported repository fail no matter what the model did.
    baseline = _baseline_dirty(controller_store, task.id)
    new_dirty = [path for path in dirty_paths if path not in baseline]
    if new_dirty:
        listed = ", ".join(new_dirty[:20])
        suffix = "" if len(new_dirty) <= 20 else f" (+{len(new_dirty) - 20} more)"
        raise TaskOperationError(
            409,
            f"Task branch has uncommitted changes: {listed}{suffix}",
        )
    if head_commit is None:
        raise TaskOperationError(
            409,
            f"Task branch '{task.branch}' has no head commit",
        )
    if head_commit == task.base_commit:
        raise TaskOperationError(
            409,
            f"Task branch '{task.branch}' has no commit beyond {task.base_commit}",
        )

    transition_task(
        controller_store,
        task_id=task.id,
        to_status=TaskStatus.REPORTED,
        head_commit=head_commit,
    )
    return _task(controller_store, task.id)


def accept_task(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    task_id: str,
) -> Task:
    """Fast-forwards the sandbox branch onto the reviewed commit, or refuses.

    This is the only code path that can destroy committed work, so every step
    is a refusal by default: the merge is `--ff-only`, the task branch head
    must still be the commit the human reviewed, and the worktree must be
    clean. Git runs before the status moves, because a merge that happened
    with a status that did not follow converges on a retry, while a status
    that moved without the merge cannot.
    """
    task = _task(controller_store, _validated_task_id(task_id))
    if task.status is TaskStatus.ACCEPTED:
        return task
    if task.status is not TaskStatus.REVIEW:
        raise TaskOperationError(
            409,
            f"Task '{task.id}' is not in review (status '{task.status.value}')",
        )
    head_commit = task.head_commit or ""
    if not _COMMIT_PATTERN.match(head_commit):
        raise TaskOperationError(409, f"Task '{task.id}' has no reviewed head commit")

    sandbox = _sandbox_for_task(controller_store, task)
    base_branch = _base_branch(task)
    output = run_git(
        docker_client,
        image=get_git_settings().git_image,
        volumes={str(sandbox["volume_name"]): {"bind": "/project", "mode": "rw"}},
        script=_accept_script(
            task.branch,
            base_branch,
            head_commit,
            sorted(_baseline_dirty(controller_store, task.id)),
        ),
    )
    result, fields, dirty = _parse_settle(output)
    _raise_accept_refusal(task, base_branch, head_commit, result, fields, dirty)

    # The merge is done. Anything below can fail without losing committed work:
    # every commit that was reachable before is reachable from base_branch now.
    settled = transition_task(
        controller_store,
        task_id=task.id,
        to_status=TaskStatus.ACCEPTED,
    )
    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.accepted",
        payload={
            "base_branch": base_branch,
            "base_head": fields.get("base", ""),
            "head_commit": head_commit,
            "already_merged": result == "already-merged",
            "agent_run_id": task.agent_run_id,
        },
    )
    if not settled:
        current = _task(controller_store, task.id)
        if current.status is not TaskStatus.ACCEPTED:
            raise TaskOperationError(
                409,
                (
                    f"Task '{task.id}' merged into '{base_branch}' but moved to "
                    f"'{current.status.value}' before it could be recorded as "
                    "accepted; no commit was lost"
                ),
            )
    stop_task_preview(
        docker_client,
        controller_store,
        task.id,
        task.sandbox_id,
        sandbox,
    )
    return _task(controller_store, task.id)


def reject_task(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    task_id: str,
) -> Task:
    """Returns the sandbox to its own branch and deletes the task branch.

    Works from `open` as well as `review`: an agent that never commits would
    otherwise hold the sandbox's only task slot forever. A branch that was
    never created, or was already deleted, is a completed reject, not an error.
    """
    task = _task(controller_store, _validated_task_id(task_id))
    if task.status is TaskStatus.REJECTED:
        return task
    if task.status not in {
        TaskStatus.OPEN,
        TaskStatus.REPORTED,
        TaskStatus.REVIEW,
    }:
        raise TaskOperationError(
            409,
            f"Task '{task.id}' cannot be rejected (status '{task.status.value}')",
        )

    sandbox = _sandbox_for_task(controller_store, task)
    base_branch = _base_branch(task)
    output = run_git(
        docker_client,
        image=get_git_settings().git_image,
        volumes={str(sandbox["volume_name"]): {"bind": "/project", "mode": "rw"}},
        script=_reject_script(task.branch, base_branch),
    )
    result, fields, details = _parse_settle(output)
    _raise_reject_refusal(task, base_branch, result, details)

    settled = transition_task(
        controller_store,
        task_id=task.id,
        to_status=TaskStatus.REJECTED,
    )
    controller_store.event(
        sandbox_id=task.sandbox_id,
        run_id=task.id,
        kind="task.rejected",
        payload={
            "base_branch": base_branch,
            "base_head": fields.get("base", ""),
            "branch_existed": result == "deleted",
            "agent_run_id": task.agent_run_id,
        },
    )
    if not settled:
        current = _task(controller_store, task.id)
        if current.status is not TaskStatus.REJECTED:
            raise TaskOperationError(
                409,
                (
                    f"Task '{task.id}' branch was deleted but the task moved to "
                    f"'{current.status.value}' before it could be recorded as "
                    "rejected"
                ),
            )
    stop_task_preview(
        docker_client,
        controller_store,
        task.id,
        task.sandbox_id,
        sandbox,
    )
    return _task(controller_store, task.id)


def open_task_for_sandbox(
    controller_store: ControllerStore,
    sandbox_id: str,
) -> Task | None:
    row = controller_store.open_task(
        sandbox_id,
        open_statuses=[status.value for status in OPEN_TASK_STATUSES],
    )
    return Task.model_validate(row) if row is not None else None


def _resolve_sandbox(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> tuple[ProjectRegistration, str]:
    try:
        project = inspect_registered_project(
            docker_client,
            project_name,
            controller_store,
        )
    except ProjectOperationError as error:
        raise TaskOperationError(error.status_code, error.detail) from error
    sandbox_id, _, project = ensure_sandbox_registered(
        docker_client,
        controller_store,
        project_name,
        project=project,
    )
    return project, sandbox_id


def _task(controller_store: ControllerStore, task_id: str) -> Task:
    row = controller_store.task(task_id)
    if row is None:
        raise TaskOperationError(404, f"Task '{task_id}' is unknown")
    return Task.model_validate(row)


def _sandbox_for_task(
    controller_store: ControllerStore,
    task: Task,
) -> dict[str, object]:
    sandbox = controller_store.sandbox(task.sandbox_id)
    if sandbox is None:
        raise TaskOperationError(404, f"Sandbox '{task.sandbox_id}' is unknown")
    return sandbox


def _base_branch(task: Task) -> str:
    """The branch a task settles onto, from trusted metadata only.

    Recorded when the controller cut the task branch. A sandbox imported from
    a host repository keeps that repository's branch, which is often not
    `main`, so the name is never assumed. Rows written before the column
    existed fall back to `main`, and a missing branch is refused rather than
    guessed from sandbox state a coding agent can change.
    """
    return _validated_branch(
        (task.base_branch or DEFAULT_BASE_BRANCH).strip(),
        task_id=task.id,
    )


def _validated_branch(name: str, *, task_id: str) -> str:
    # The name reaches a shell script inside double quotes. Git itself allows
    # characters that would end that quoting, so the accepted set is a
    # whitelist rather than an escape.
    if not _BRANCH_PATTERN.match(name) or ".." in name or name.endswith(".lock"):
        raise TaskOperationError(
            409,
            f"Task '{task_id}' has an unusable base branch '{name}'",
        )
    return name


def _validated_task_id(task_id: str) -> str:
    # Task identifiers reach a shell script as a branch name, so anything
    # outside the generated alphabet is refused before it gets there.
    if not _TASK_ID_PATTERN.match(task_id):
        raise TaskOperationError(404, f"Task '{task_id}' is unknown")
    return task_id


def _branch_script(branch: str, base_commit: str) -> str:
    # The base branch is read before the switch, because after it HEAD names
    # the task branch. Accept and reject merge into and return to this branch,
    # so a task that cannot name one is refused here rather than at settlement.
    return (
        "set -eu\n"
        "cd /project\n"
        'head="$(git rev-parse --verify HEAD)"\n'
        f'if [ "$head" != "{base_commit}" ]; then\n'
        '  echo "sandbox head moved to $head" >&2\n'
        "  exit 1\n"
        "fi\n"
        'base_branch="$(git symbolic-ref --quiet --short HEAD || echo "")"\n'
        'if [ -z "$base_branch" ]; then\n'
        '  echo "sandbox HEAD is detached; a task needs a branch to settle onto" >&2\n'
        "  exit 1\n"
        "fi\n"
        'printf "base-branch %s\\n" "$base_branch"\n'
        # Record a fingerprinted sandbox baseline for feature delivery checks.
        + snapshot_shell()
        + f'git switch -c "{branch}"\n'
        f'git rev-parse --verify "refs/heads/{branch}"\n'
    )


def _report_script(branch: str) -> str:
    return (
        "set -eu\n"
        "cd /project\n"
        f'printf "head %s\\n" "$(git rev-parse --verify "refs/heads/{branch}")"\n'
        'printf "branch %s\\n" "$(git symbolic-ref --quiet --short HEAD || echo "")"\n'
        "git status --porcelain | while IFS= read -r entry; do\n"
        '  printf "dirty %s\\n" "$entry"\n'
        "done\n"
    )


def _accept_script(
    branch: str,
    base_branch: str,
    head_commit: str,
    baseline_dirty: Sequence[str] = (),
) -> str:
    """Refuses first, mutates last, and repeats safely.

    Every condition that should return 409 prints a result line and exits 0,
    so the caller can tell a refusal from a broken container. The mutating
    three commands only run once nothing is left to refuse, which keeps the
    checkout untouched when the merge would not have been a fast-forward.

    `baseline_dirty` is what was already uncommitted when the branch was cut.
    Those paths are excluded from the dirty refusal for the same reason
    `report_task` excludes them: a sandbox imported from a real repository
    carries untracked files the task never touched, and refusing on them makes
    every task in that sandbox impossible to settle.
    """
    return (
        "set -eu\n"
        "cd /project\n"
        f'base_head="$(git rev-parse --verify --quiet "refs/heads/{base_branch}" || true)"\n'
        'if [ -z "$base_head" ]; then\n'
        '  printf "result missing-base-branch\\n"\n'
        "  exit 0\n"
        "fi\n"
        f'task_head="$(git rev-parse --verify --quiet "refs/heads/{branch}" || true)"\n'
        'if [ -z "$task_head" ]; then\n'
        f'  if git merge-base --is-ancestor "{head_commit}" "refs/heads/{base_branch}" 2>/dev/null; then\n'
        '    printf "result already-merged\\n"\n'
        '    printf "base %s\\n" "$base_head"\n'
        "    exit 0\n"
        "  fi\n"
        '  printf "result missing-task-branch\\n"\n'
        "  exit 0\n"
        "fi\n"
        f'if [ "$task_head" != "{head_commit}" ]; then\n'
        '  printf "result branch-moved\\n"\n'
        '  printf "task %s\\n" "$task_head"\n'
        "  exit 0\n"
        "fi\n"
        # Keep the path normalisation in step with `_dirty_path`: strip the
        # two-column status and its space, then take the destination half of a
        # rename, then unquote. A baseline entry only matches what git would
        # print for that same path today.
        f"baseline={shlex.quote(chr(10).join(baseline_dirty))}\n"
        'new_dirty=""\n'
        "git status --porcelain | while IFS= read -r entry; do\n"
        '  [ -n "$entry" ] || continue\n'
        '  path="${entry#???}"\n'
        '  case "$path" in *" -> "*) path="${path##* -> }";; esac\n'
        '  path="${path#\\"}"\n'
        '  path="${path%\\"}"\n'
        '  if ! printf "%s\\n" "$baseline" | grep -qxF -- "$path"; then\n'
        '    printf "%s\\n" "$entry"\n'
        "  fi\n"
        "done > /tmp/new_dirty\n"
        "if [ -s /tmp/new_dirty ]; then\n"
        '  printf "result dirty\\n"\n'
        "  while IFS= read -r entry; do\n"
        '    printf "detail %s\\n" "$entry"\n'
        "  done < /tmp/new_dirty\n"
        "  exit 0\n"
        "fi\n"
        f'if ! git merge-base --is-ancestor "refs/heads/{base_branch}" "$task_head"; then\n'
        '  printf "result diverged\\n"\n'
        '  printf "base %s\\n" "$base_head"\n'
        '  printf "task %s\\n" "$task_head"\n'
        f'  printf "counts %s\\n" "$(git rev-list --left-right --count "refs/heads/{base_branch}...$task_head" | tr "\\t" " ")"\n'
        "  exit 0\n"
        "fi\n"
        f'git switch --quiet "{base_branch}"\n'
        f'git merge --ff-only --quiet "{branch}"\n'
        f'git branch -d "{branch}"\n'
        'printf "result merged\\n"\n'
        'printf "base %s\\n" "$(git rev-parse --verify HEAD)"\n'
    )


def _reject_script(branch: str, base_branch: str) -> str:
    # No force flag anywhere except `branch -D`, which is the whole point of a
    # reject. A switch that git refuses is reported, never overridden: the
    # refusal only happens when uncommitted work would be overwritten.
    return (
        "set -eu\n"
        "cd /project\n"
        f'base_head="$(git rev-parse --verify --quiet "refs/heads/{base_branch}" || true)"\n'
        'if [ -z "$base_head" ]; then\n'
        '  printf "result missing-base-branch\\n"\n'
        "  exit 0\n"
        "fi\n"
        'current="$(git symbolic-ref --quiet --short HEAD || echo "")"\n'
        f'if [ "$current" != "{base_branch}" ]; then\n'
        f'  switch_error="$(git switch --quiet "{base_branch}" 2>&1)" || {{\n'
        '    printf "result switch-failed\\n"\n'
        '    printf "%s\\n" "$switch_error" | while IFS= read -r entry; do\n'
        '      printf "detail %s\\n" "$entry"\n'
        "    done\n"
        "    exit 0\n"
        "  }\n"
        "fi\n"
        f'if git rev-parse --verify --quiet "refs/heads/{branch}" >/dev/null; then\n'
        f'  git branch -D "{branch}"\n'
        '  printf "result deleted\\n"\n'
        "else\n"
        '  printf "result missing-task-branch\\n"\n'
        "fi\n"
        f'printf "base %s\\n" "$(git rev-parse --verify "refs/heads/{base_branch}")"\n'
    )


def _parse_report(output: bytes) -> tuple[str | None, str, list[str]]:
    head: str | None = None
    checked_out = ""
    dirty: list[str] = []
    for line in output.decode(errors="replace").splitlines():
        if line.startswith("head "):
            candidate = line[5:].strip()
            head = candidate if _COMMIT_PATTERN.match(candidate) else None
        elif line.startswith("branch "):
            checked_out = line[7:].strip()
        elif line.startswith("dirty "):
            dirty.append(_dirty_path(line[6:]))
    return head, checked_out, dirty


def _parse_base_branch(output: bytes) -> str:
    for line in output.decode(errors="replace").splitlines():
        if line.startswith("base-branch "):
            return line[12:].strip()
    return ""


def _parse_dirty(output: bytes) -> list[str]:
    return [
        _dirty_path(line[6:])
        for line in output.decode(errors="replace").splitlines()
        if line.startswith("dirty ")
    ]


def _baseline_dirty(controller_store: ControllerStore, task_id: str) -> set[str]:
    """What git already called dirty when this task's branch was cut.

    An empty set for a task opened before the snapshot existed, which keeps the
    old, stricter behaviour for those rather than inventing a baseline.
    """
    row = controller_store.task(task_id) or {}
    try:
        parsed = json.loads(str(row.get("baseline_dirty_json") or "[]"))
    except ValueError:
        return set()
    return {str(path) for path in parsed} if isinstance(parsed, list) else set()


def _parse_settle(output: bytes) -> tuple[str, dict[str, str], list[str]]:
    result = ""
    fields: dict[str, str] = {}
    details: list[str] = []
    for line in output.decode(errors="replace").splitlines():
        if line.startswith("result "):
            result = line[7:].strip()
        elif line.startswith("detail "):
            details.append(line[7:].strip())
        else:
            for key in ("base", "task", "counts"):
                if line.startswith(f"{key} "):
                    fields[key] = line[len(key) + 1 :].strip()
                    break
    return result, fields, details


def _raise_accept_refusal(
    task: Task,
    base_branch: str,
    head_commit: str,
    result: str,
    fields: dict[str, str],
    details: list[str],
) -> None:
    if result in {"merged", "already-merged"}:
        return
    if result == "missing-base-branch":
        raise TaskOperationError(
            409,
            f"Sandbox branch '{base_branch}' does not exist; nothing to merge into",
        )
    if result == "missing-task-branch":
        raise TaskOperationError(
            409,
            (
                f"Task branch '{task.branch}' is gone and '{base_branch}' does not "
                f"contain {head_commit}"
            ),
        )
    if result == "branch-moved":
        raise TaskOperationError(
            409,
            (
                f"Task branch '{task.branch}' moved to {fields.get('task', 'unknown')} "
                f"since review; the reviewed commit was {head_commit}"
            ),
        )
    if result == "dirty":
        raise TaskOperationError(
            409,
            f"Sandbox worktree has uncommitted changes: {_listed(details)}",
        )
    if result == "diverged":
        behind, _, ahead = fields.get("counts", "").partition(" ")
        raise TaskOperationError(
            409,
            (
                f"Sandbox branch '{base_branch}' at {fields.get('base', 'unknown')} "
                f"has diverged from task branch '{task.branch}' at "
                f"{fields.get('task', 'unknown')}: {behind or '?'} commit(s) only on "
                f"'{base_branch}', {ahead or '?'} only on the task branch. "
                "A fast-forward merge is not possible"
            ),
        )
    raise TaskOperationError(502, f"Sandbox git returned no accept result: {result!r}")


def _raise_reject_refusal(
    task: Task,
    base_branch: str,
    result: str,
    details: list[str],
) -> None:
    if result in {"deleted", "missing-task-branch"}:
        return
    if result == "missing-base-branch":
        raise TaskOperationError(
            409,
            f"Sandbox branch '{base_branch}' does not exist; cannot leave "
            f"'{task.branch}'",
        )
    if result == "switch-failed":
        raise TaskOperationError(
            409,
            (
                f"Sandbox cannot return to '{base_branch}' without overwriting "
                f"uncommitted work: {_listed(details)}"
            ),
        )
    raise TaskOperationError(502, f"Sandbox git returned no reject result: {result!r}")


def _listed(entries: list[str]) -> str:
    shown = ", ".join(entry for entry in entries[:20])
    suffix = "" if len(entries) <= 20 else f" (+{len(entries) - 20} more)"
    return f"{shown}{suffix}"


def _dirty_path(entry: str) -> str:
    path = entry[3:] if len(entry) > 3 else entry.strip()
    # A rename reads "R  old -> new"; the destination is the interesting half.
    return path.split(" -> ")[-1].strip().strip('"')
