"""Commit-pinned feature diffs and delivery to the original source folder."""

import re
import shlex
from dataclasses import dataclass
from typing import Any

from docker.client import DockerClient

from app.controller.store import ControllerStore
from app.delegation import service
from app.delegation.models import (
    DelegationStatus,
    DelegationView,
    FeatureDiff,
    FeatureDiffFile,
    IntegrationReviewStatus,
    RunStatus,
)
from app.platform.dirty_state import (
    DirtyEntry,
    deserialize_snapshot,
    legacy_paths,
    parse_snapshot,
    serialize_snapshot,
    snapshot_shell,
)
from app.previews.config import PreviewSettings
from app.sandboxes.git import run_git
from app.tasks.models import TaskStatus


_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
MAX_PATCH_BYTES = 500_000
MAX_NUMSTAT_BYTES = 100_000
_INCORPORATED_CHANGE_STATUSES = {"awaiting_review", "completed"}


@dataclass(frozen=True)
class FeatureTarget:
    base_branch: str
    base_commit: str
    head_commit: str


@dataclass(frozen=True)
class SandboxState:
    branch: str
    head: str
    dirty: list[DirtyEntry] | None
    legacy_dirty: bool


def capture_feature_target(
    docker_client: DockerClient,
    settings: PreviewSettings,
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
    state = _sandbox_state(
        docker_client,
        settings.git_image,
        str(sandbox["volume_name"]),
    )
    _ensure_original_dirty_state(
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


def ensure_target_unchanged(
    docker_client: DockerClient,
    settings: PreviewSettings,
    store: ControllerStore,
    sandbox_id: str,
    target: FeatureTarget,
) -> None:
    """Refuses a review result when its repository changed during the turn."""
    sandbox = store.sandbox(sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(404, "Delegation sandbox was not found")
    state = _sandbox_state(
        docker_client,
        settings.git_image,
        str(sandbox["volume_name"]),
    )
    _ensure_original_dirty_state(store, sandbox_id, sandbox, state)
    if state.branch != target.base_branch or state.head != target.head_commit:
        raise service.DelegationOperationError(
            409,
            "Sandbox code changed while the feature review was running; run the review again",
        )


def feature_diff(
    docker_client: DockerClient,
    settings: PreviewSettings,
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
        target = FeatureTarget(review.base_branch, review.base_commit, review.head_commit)
        review_id: str | None = review.id
        sandbox = store.sandbox(delegation_view.delegation.sandbox_id)
        if sandbox is None:
            raise service.DelegationOperationError(404, "Delegation sandbox was not found")
        state = _sandbox_state(
            docker_client,
            settings.git_image,
            str(sandbox["volume_name"]),
        )
        _ensure_original_dirty_state(
            store,
            delegation_view.delegation.sandbox_id,
            sandbox,
            state,
        )
    else:
        target = capture_feature_target(
            docker_client,
            settings,
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
        settings.git_image,
        volume_name,
        target,
        "--numstat --no-renames",
        MAX_NUMSTAT_BYTES,
    )
    raw_patch = _bounded_diff(
        docker_client,
        settings.git_image,
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
        raise service.DelegationOperationError(409, "Delegation has no accepted task commits")
    branches = {str(task.get("base_branch") or "") for task in tasks}
    if len(branches) != 1:
        raise service.DelegationOperationError(409, "Feature tasks used different base branches")
    branch = branches.pop()
    if not _BRANCH_PATTERN.fullmatch(branch):
        raise service.DelegationOperationError(409, "Feature base branch is unusable")

    by_base: dict[str, dict[str, Any]] = {}
    heads: set[str] = set()
    for task in tasks:
        base = str(task.get("base_commit") or "")
        head = str(task.get("head_commit") or "")
        if not _COMMIT_PATTERN.fullmatch(base) or not _COMMIT_PATTERN.fullmatch(head):
            raise service.DelegationOperationError(409, "Feature task has no verified commit range")
        if base in by_base:
            raise service.DelegationOperationError(
                409,
                "Feature task commits do not form one chain",
            )
        by_base[base] = task
        heads.add(head)

    starts = [base for base in by_base if base not in heads]
    if len(starts) != 1:
        raise service.DelegationOperationError(409, "Feature task commits do not form one chain")
    base_commit = starts[0]
    head_commit = base_commit
    visited = 0
    while head_commit in by_base:
        task = by_base[head_commit]
        head_commit = str(task["head_commit"])
        visited += 1
    if visited != len(tasks):
        raise service.DelegationOperationError(409, "Feature task commits do not form one chain")
    return FeatureTarget(branch, base_commit, head_commit)


def _sandbox_state(
    docker_client: DockerClient,
    git_image: str,
    volume_name: str,
) -> SandboxState:
    script = (
        "set -eu\n"
        "cd /project\n"
        'printf "branch %s\\n" "$(git symbolic-ref --quiet --short HEAD || true)"\n'
        'printf "head %s\\n" "$(git rev-parse --verify HEAD)"\n'
        + snapshot_shell()
    )
    output = run_git(
        docker_client,
        image=git_image,
        volumes={volume_name: {"bind": "/project", "mode": "ro"}},
        script=script,
    )
    fields = _fields(output)
    dirty = parse_snapshot(output)
    return SandboxState(
        branch=fields.get("branch", ""),
        head=fields.get("head", ""),
        dirty=dirty,
        legacy_dirty=fields.get("dirty") == "true",
    )


def _ensure_original_dirty_state(
    store: ControllerStore,
    sandbox_id: str,
    sandbox: dict[str, Any],
    state: SandboxState,
) -> None:
    """Allow only the exact dirty entries present before delegated work."""
    if state.dirty is None:
        if state.legacy_dirty:
            raise service.DelegationOperationError(
                409,
                "Sandbox worktree is dirty, but Git did not return path fingerprints",
            )
        current: list[DirtyEntry] = []
    else:
        current = state.dirty

    raw_baseline = sandbox.get("dirty_baseline_json")
    baseline = deserialize_snapshot(raw_baseline)
    if raw_baseline and baseline is None:
        raise service.DelegationOperationError(
            409,
            "Sandbox dirty baseline has an unsupported or invalid format",
        )
    if baseline is None:
        baseline = _seed_legacy_baseline(store, sandbox_id, current)
        setter = getattr(store, "set_sandbox_dirty_baseline_if_missing", None)
        if setter is not None:
            written = setter(
                sandbox_id=sandbox_id,
                baseline_json=serialize_snapshot(baseline),
            )
            if not written:
                refreshed = store.sandbox(sandbox_id)
                concurrent = deserialize_snapshot(
                    (refreshed or {}).get("dirty_baseline_json")
                )
                if concurrent is None:
                    raise service.DelegationOperationError(
                        409,
                        "Sandbox dirty baseline changed during the safety check",
                    )
                baseline = concurrent

    blockers = _dirty_blockers(baseline, current)
    if blockers:
        raise service.DelegationOperationError(
            409,
            "Sandbox worktree differs from its original dirty baseline: "
            + "; ".join(blockers),
        )


def _seed_legacy_baseline(
    store: ControllerStore,
    sandbox_id: str,
    current: list[DirtyEntry],
) -> list[DirtyEntry]:
    """Upgrade one active sandbox from path-only task history.

    The fallback accepts no path outside the earliest recorded task baseline.
    Each recorded path must still exist. Once stored, all later checks use
    exact status, type, and content fingerprints.
    """
    rows_method = getattr(store, "tasks_for_sandbox", None)
    rows = rows_method(sandbox_id) if rows_method is not None else []
    recorded: list[str] | None = None
    for row in rows:
        candidate = legacy_paths(row.get("baseline_dirty_json"))
        if candidate is not None:
            recorded = candidate
            break
    recorded = recorded or []

    outside = [entry for entry in current if not _covered_by_legacy(entry.path, recorded)]
    missing = [
        path
        for path in recorded
        if not any(_legacy_path_covers(path, entry.path) for entry in current)
    ]
    blockers = [
        f"{entry.path}: new uncommitted {entry.file_type} (Git status {entry.status!r})"
        for entry in outside
    ]
    blockers.extend(f"{path}: removed pre-existing dirty path" for path in missing)
    if blockers:
        raise service.DelegationOperationError(
            409,
            "Sandbox worktree cannot safely upgrade its legacy dirty baseline: "
            + "; ".join(blockers),
        )
    return current


def _covered_by_legacy(path: str, recorded: list[str]) -> bool:
    return any(_legacy_path_covers(baseline_path, path) for baseline_path in recorded)


def _legacy_path_covers(baseline_path: str, current_path: str) -> bool:
    return current_path == baseline_path or (
        baseline_path.endswith("/") and current_path.startswith(baseline_path)
    )


def _dirty_blockers(
    baseline: list[DirtyEntry],
    current: list[DirtyEntry],
) -> list[str]:
    before = {entry.path: entry for entry in baseline}
    after = {entry.path: entry for entry in current}
    blockers: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        original = before.get(path)
        present = after.get(path)
        if original is None and present is not None:
            blockers.append(
                f"{path}: new uncommitted {present.file_type} "
                f"(Git status {present.status!r})"
            )
            continue
        if present is None and original is not None:
            blockers.append(
                f"{path}: removed pre-existing dirty {original.file_type} "
                f"(Git status was {original.status!r})"
            )
            continue
        if original is None or present is None:  # pragma: no cover - narrowed above
            continue
        changes: list[str] = []
        if original.status != present.status:
            changes.append(f"Git status changed from {original.status!r} to {present.status!r}")
        if original.file_type != present.file_type:
            changes.append(
                f"file type changed from {original.file_type} to {present.file_type}"
            )
        elif original.file_type in {"file", "symlink"}:
            if original.fingerprint is None or present.fingerprint is None:
                changes.append("content fingerprint is unavailable")
            elif original.fingerprint != present.fingerprint:
                changes.append("content modified")
        if changes:
            blockers.append(f"{path}: {', '.join(changes)}")
    return blockers


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
        raise service.DelegationOperationError(409, "Feature review has an invalid Git target")


def _fields(output: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition(" ")
        if separator and key in {"branch", "dirty", "head", "result"}:
            fields[key] = value.strip()
    return fields
