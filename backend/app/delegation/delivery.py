"""Commit-pinned feature diffs and delivery to the original source folder."""

import re
import shlex
from typing import Any

from docker.client import DockerClient

from app.containers.git import run_git
from app.controller.store import ControllerStore
from app.controller.store.delegation_status import DelegationStatus
from app.controller.store.task_status import TaskStatus
from app.delegation import service
from app.delegation.models import (
    DelegationView,
    FeatureDiff,
    FeatureDiffFile,
    RunStatus,
)
from app.sandboxes.feature_target import (
    FeatureTarget,
    FeatureTargetError,
    SandboxState,
    ensure_original_dirty_state,
    sandbox_state,
)

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
MAX_PATCH_BYTES = 500_000
MAX_NUMSTAT_BYTES = 100_000
_INCORPORATED_CHANGE_STATUSES = {"awaiting_review", "completed"}


def _ensure_dirty_state(
    store: ControllerStore,
    sandbox_id: str,
    sandbox: dict[str, Any],
    state: SandboxState,
) -> None:
    """Raise the delegation error for a sandboxes-owned dirty-state refusal.

    Both public entry points here reach the same check, so the conversion
    lives in one place rather than at each call.
    """
    try:
        ensure_original_dirty_state(store, sandbox_id, sandbox, state)
    except FeatureTargetError as error:
        raise service.DelegationOperationError(
            error.status_code, error.detail
        ) from error


def capture_feature_target(
    docker_client: DockerClient,
    git_image: str,
    store: ControllerStore,
    delegation_view: DelegationView,
) -> FeatureTarget:
    """Resolves the accepted task chain and confirms the sandbox matches it."""
    if delegation_view.delegation.status is not DelegationStatus.COMPLETED:
        raise service.DelegationOperationError(
            409,
            "A feature diff requires a completed delegation",
        )
    tasks: list[dict[str, Any]] = []
    for entry in delegation_view.items:
        succeeded = next(
            (run for run in entry.runs if run.status is RunStatus.SUCCEEDED),
            None,
        )
        task_id = succeeded.task_id if succeeded else None
        row = store.task(task_id) if task_id else None
        if row is None or str(row.get("status")) != TaskStatus.ACCEPTED.value:
            raise service.DelegationOperationError(
                409,
                f"Work item '{entry.item.key}' has no accepted task commit",
            )
        tasks.append(row)
    for change in getattr(delegation_view, "changes", []):
        if (
            change.status.value not in _INCORPORATED_CHANGE_STATUSES
            or not change.task_id
        ):
            continue
        row = store.task(change.task_id)
        if row is None or str(row.get("status")) != TaskStatus.ACCEPTED.value:
            raise service.DelegationOperationError(
                409,
                f"Change request {change.revision} has no accepted task commit",
            )
        tasks.append(row)

    target = _task_chain(tasks)
    sandbox = store.sandbox(delegation_view.delegation.sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(404, "Delegation sandbox was not found")
    state = sandbox_state(
        docker_client,
        git_image,
        str(sandbox["volume_name"]),
    )
    _ensure_dirty_state(
        store,
        delegation_view.delegation.sandbox_id,
        sandbox,
        state,
    )
    if state.branch != target.base_branch:
        raise service.DelegationOperationError(
            409,
            f"Sandbox is on branch '{state.branch or 'detached HEAD'}', "
            f"not '{target.base_branch}'",
        )
    if state.head != target.head_commit:
        raise service.DelegationOperationError(
            409,
            "Sandbox HEAD changed after the final work item was accepted",
        )
    return target


def feature_diff(
    docker_client: DockerClient,
    git_image: str,
    store: ControllerStore,
    delegation_view: DelegationView,
) -> FeatureDiff:
    """Returns a bounded Git patch for the latest pinned or current target."""
    review = delegation_view.review
    changes = getattr(delegation_view, "changes", [])
    review_is_current = bool(
        review
        and not any(
            change.status.value in _INCORPORATED_CHANGE_STATUSES
            and review.settled_at
            and change.created_at > review.settled_at
            for change in changes
        )
    )
    if (
        review
        and review.base_branch
        and review.base_commit
        and review.head_commit
        and review_is_current
    ):
        target = FeatureTarget(
            review.base_branch, review.base_commit, review.head_commit
        )
        review_id: str | None = review.id
        sandbox = store.sandbox(delegation_view.delegation.sandbox_id)
        if sandbox is None:
            raise service.DelegationOperationError(
                404, "Delegation sandbox was not found"
            )
        state = sandbox_state(
            docker_client,
            git_image,
            str(sandbox["volume_name"]),
        )
        _ensure_dirty_state(
            store,
            delegation_view.delegation.sandbox_id,
            sandbox,
            state,
        )
    else:
        target = capture_feature_target(
            docker_client,
            git_image,
            store,
            delegation_view,
        )
        review_id = None

    sandbox = store.sandbox(delegation_view.delegation.sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(404, "Delegation sandbox was not found")
    volume_name = str(sandbox["volume_name"])
    _validate_target(target)
    numstat = _bounded_diff(
        docker_client,
        git_image,
        volume_name,
        target,
        "--numstat --no-renames",
        MAX_NUMSTAT_BYTES,
    )
    raw_patch = _bounded_diff(
        docker_client,
        git_image,
        volume_name,
        target,
        "--no-ext-diff --find-renames --unified=3",
        MAX_PATCH_BYTES + 1,
    )
    truncated = len(raw_patch) > MAX_PATCH_BYTES
    patch = raw_patch[:MAX_PATCH_BYTES].decode("utf-8", errors="replace")
    files = _parse_numstat(numstat)
    return FeatureDiff(
        review_id=review_id,
        base_branch=target.base_branch,
        base_commit=target.base_commit,
        head_commit=target.head_commit,
        files=files,
        additions=sum(entry.additions or 0 for entry in files),
        deletions=sum(entry.deletions or 0 for entry in files),
        patch=patch,
        truncated=truncated,
    )


def _task_chain(tasks: list[dict[str, Any]]) -> FeatureTarget:
    if not tasks:
        raise service.DelegationOperationError(
            409, "Delegation has no accepted task commits"
        )
    branches = {str(task.get("base_branch") or "") for task in tasks}
    if len(branches) != 1:
        raise service.DelegationOperationError(
            409, "Feature tasks used different base branches"
        )
    branch = branches.pop()
    if not _BRANCH_PATTERN.fullmatch(branch):
        raise service.DelegationOperationError(409, "Feature base branch is unusable")

    by_base: dict[str, dict[str, Any]] = {}
    heads: set[str] = set()
    for task in tasks:
        base = str(task.get("base_commit") or "")
        head = str(task.get("head_commit") or "")
        if not _COMMIT_PATTERN.fullmatch(base) or not _COMMIT_PATTERN.fullmatch(head):
            raise service.DelegationOperationError(
                409, "Feature task has no verified commit range"
            )
        if base in by_base:
            raise service.DelegationOperationError(
                409,
                "Feature task commits do not form one chain",
            )
        by_base[base] = task
        heads.add(head)

    starts = [base for base in by_base if base not in heads]
    if len(starts) != 1:
        raise service.DelegationOperationError(
            409, "Feature task commits do not form one chain"
        )
    base_commit = starts[0]
    head_commit = base_commit
    visited = 0
    while head_commit in by_base:
        task = by_base[head_commit]
        head_commit = str(task["head_commit"])
        visited += 1
    if visited != len(tasks):
        raise service.DelegationOperationError(
            409, "Feature task commits do not form one chain"
        )
    return FeatureTarget(branch, base_commit, head_commit)


def _bounded_diff(
    docker_client: DockerClient,
    git_image: str,
    volume_name: str,
    target: FeatureTarget,
    arguments: str,
    limit: int,
) -> bytes:
    script = (
        "set -eu\n"
        "cd /project\n"
        f"git cat-file -e {shlex.quote(target.base_commit + '^{commit}')}\n"
        f"git cat-file -e {shlex.quote(target.head_commit + '^{commit}')}\n"
        f"git merge-base --is-ancestor {shlex.quote(target.base_commit)} "
        f"{shlex.quote(target.head_commit)}\n"
        f"git diff {arguments} {shlex.quote(target.base_commit)} "
        f"{shlex.quote(target.head_commit)} | head -c {limit}\n"
    )
    return run_git(
        docker_client,
        image=git_image,
        volumes={volume_name: {"bind": "/project", "mode": "ro"}},
        script=script,
    )


def _parse_numstat(output: bytes) -> list[FeatureDiffFile]:
    files: list[FeatureDiffFile] = []
    for raw in output.decode("utf-8", errors="replace").splitlines():
        additions, separator, rest = raw.partition("\t")
        if not separator:
            continue
        deletions, separator, path = rest.partition("\t")
        if not separator or not path:
            continue
        binary = additions == "-" or deletions == "-"
        try:
            added_count = None if binary else int(additions)
            deleted_count = None if binary else int(deletions)
        except ValueError:
            continue
        files.append(
            FeatureDiffFile(
                path=path,
                additions=added_count,
                deletions=deleted_count,
                binary=binary,
            )
        )
    return files


def _validate_target(target: FeatureTarget) -> None:
    if (
        not _BRANCH_PATTERN.fullmatch(target.base_branch)
        or not _COMMIT_PATTERN.fullmatch(target.base_commit)
        or not _COMMIT_PATTERN.fullmatch(target.head_commit)
    ):
        raise service.DelegationOperationError(
            409, "Feature review has an invalid Git target"
        )
