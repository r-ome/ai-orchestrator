"""Sandbox git state behind a pinned feature target.

`ensure_target_unchanged` reads a sandbox volume and compares its branch,
HEAD and dirty baseline. That is sandbox state, not delegation state, so it
lives here and delegation imports it. Keeping it in `delegation/delivery.py`
put `sandboxes -> delegation` into the app import cycle for three symbols.
"""

from dataclasses import dataclass
from typing import Any

from docker.client import DockerClient

from app.controller.store import ControllerStore
from app.platform.dirty_state import (
    DirtyEntry,
    deserialize_snapshot,
    legacy_paths,
    parse_snapshot,
    serialize_snapshot,
    snapshot_shell,
)
from app.platform.errors import OperationError
from app.previews.config import PreviewSettings
from app.sandboxes.git import run_git


class FeatureTargetError(OperationError):
    """A sandbox no longer matches the feature target pinned for it."""


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
        raise FeatureTargetError(404, "Delegation sandbox was not found")
    state = sandbox_state(
        docker_client,
        settings.git_image,
        str(sandbox["volume_name"]),
    )
    ensure_original_dirty_state(store, sandbox_id, sandbox, state)
    if state.branch != target.base_branch or state.head != target.head_commit:
        raise FeatureTargetError(
            409,
            "Sandbox code changed while the feature review was running; run the review again",
        )


def sandbox_state(
    docker_client: DockerClient,
    git_image: str,
    volume_name: str,
) -> SandboxState:
    script = (
        "set -eu\n"
        "cd /project\n"
        'printf "branch %s\\n" "$(git symbolic-ref --quiet --short HEAD || true)"\n'
        'printf "head %s\\n" "$(git rev-parse --verify HEAD)"\n' + snapshot_shell()
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


def ensure_original_dirty_state(
    store: ControllerStore,
    sandbox_id: str,
    sandbox: dict[str, Any],
    state: SandboxState,
) -> None:
    """Allow only the exact dirty entries present before delegated work."""
    if state.dirty is None:
        if state.legacy_dirty:
            raise FeatureTargetError(
                409,
                "Sandbox worktree is dirty, but Git did not return path fingerprints",
            )
        current: list[DirtyEntry] = []
    else:
        current = state.dirty

    raw_baseline = sandbox.get("dirty_baseline_json")
    baseline = deserialize_snapshot(raw_baseline)
    if raw_baseline and baseline is None:
        raise FeatureTargetError(
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
                    raise FeatureTargetError(
                        409,
                        "Sandbox dirty baseline changed during the safety check",
                    )
                baseline = concurrent

    blockers = _dirty_blockers(baseline, current)
    if blockers:
        raise FeatureTargetError(
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

    outside = [
        entry for entry in current if not _covered_by_legacy(entry.path, recorded)
    ]
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
        raise FeatureTargetError(
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
            changes.append(
                f"Git status changed from {original.status!r} to {present.status!r}"
            )
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


def _fields(output: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition(" ")
        if separator and key in {"branch", "dirty", "head", "result"}:
            fields[key] = value.strip()
    return fields
