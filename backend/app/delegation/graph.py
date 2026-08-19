"""Validate work-item graphs and derive execution order."""

from collections.abc import Mapping, Sequence
from typing import Any

from app.delegation.models import Complexity, RunStatus, WorkItemState

MAX_ITEMS = 60
REQUIRED_TEXT = ("title", "objective", "scope")


def validate_work_items(
    items: Sequence[Mapping[str, Any]],
    *,
    available_command_kinds: frozenset[str] | None = None,
) -> list[str]:
    if not isinstance(items, Sequence) or not items:
        return ["the decomposition must contain at least one work item"]
    if len(items) > MAX_ITEMS:
        return [f"the decomposition must contain at most {MAX_ITEMS} work items"]

    errors: list[str] = []
    keys: list[str] = []
    seen: set[str] = set()

    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{label} must be an object")
            keys.append(f"<{index}>")
            continue

        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{label}.key is required and must be a non-empty string")
            key = None
        elif key in seen:
            errors.append(f"{label}.key '{key}' is used more than once")
        else:
            seen.add(key)
        keys.append(key or f"<{index}>")

        for field in REQUIRED_TEXT:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"{label}.{field} is required and must be a non-empty string"
                )

        complexity = item.get("complexity")
        if complexity not in {level.value for level in Complexity}:
            errors.append(
                f"{label}.complexity must be one of "
                f"{[level.value for level in Complexity]}"
            )

        criteria = item.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria:
            errors.append(f"{label}.acceptance_criteria must be a non-empty list")
        elif any(
            not isinstance(entry, str) or not entry.strip() for entry in criteria
        ):
            errors.append(
                f"{label}.acceptance_criteria must contain only non-empty strings"
            )

        errors += _verification(item, label, available_command_kinds)
        errors += _string_list(item, "dependencies", label)
        for field in ("files", "symbols", "write_scope", "architecture", "risks"):
            errors += _string_list(item, field, label)

    errors += _dependencies(items, keys, seen)
    return errors


def _string_list(item: Mapping[str, Any], field: str, label: str) -> list[str]:
    value = item.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"{label}.{field} must be a list of strings"]
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        return [f"{label}.{field} must contain only non-empty strings"]
    return []


def _verification(
    item: Mapping[str, Any],
    label: str,
    available: frozenset[str] | None,
) -> list[str]:
    intents = item.get("verification")
    if not isinstance(intents, list) or not intents:
        return [f"{label}.verification must be a non-empty list of intents"]

    errors: list[str] = []
    for index, intent in enumerate(intents):
        where = f"{label}.verification[{index}]"
        if not isinstance(intent, Mapping):
            errors.append(f"{where} must be an object with 'command_kind'")
            continue
        kind = intent.get("command_kind")
        if not isinstance(kind, str) or not kind.strip():
            errors.append(f"{where}.command_kind is required")
        elif available is not None and kind not in available:
            errors.append(
                f"{where}.command_kind '{kind}' is not confirmed for this project; "
                f"confirmed kinds are {sorted(available)}"
            )
    return errors


def _dependencies(
    items: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    known: set[str],
) -> list[str]:
    errors: list[str] = []
    edges: dict[str, list[str]] = {}

    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        key = keys[index]
        dependencies = item.get("dependencies")
        resolved: list[str] = []
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if not isinstance(dependency, str):
                    continue
                if dependency == key:
                    errors.append(f"items[{index}] '{key}' depends on itself")
                elif dependency not in known:
                    errors.append(
                        f"items[{index}] '{key}' depends on '{dependency}', "
                        "which is not a work item in this decomposition"
                    )
                else:
                    resolved.append(dependency)
        edges[key] = resolved

    cycle = find_cycle(edges)
    if cycle:
        errors.append("dependencies form a cycle: " + " -> ".join([*cycle, cycle[0]]))
    return errors


def find_cycle(edges: Mapping[str, Sequence[str]]) -> list[str]:
    """Return one cycle as a list of keys, or an empty list."""
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def walk(node: str) -> list[str]:
        if node in done:
            return []
        if node in visiting:
            start = stack.index(node)
            return stack[start:]
        visiting.add(node)
        stack.append(node)
        for neighbour in edges.get(node, ()):
            found = walk(neighbour)
            if found:
                return found
        stack.pop()
        visiting.discard(node)
        done.add(node)
        return []

    for node in edges:
        cycle = walk(node)
        if cycle:
            return cycle
    return []


def waves(edges: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """Group keys by dependency depth."""
    depth: dict[str, int] = {}

    def resolve(node: str, seen: frozenset[str]) -> int:
        if node in depth:
            return depth[node]
        if node in seen:
            return 0
        dependencies = [
            dependency for dependency in edges.get(node, ()) if dependency in edges
        ]
        value = (
            0
            if not dependencies
            else 1
            + max(resolve(dependency, seen | {node}) for dependency in dependencies)
        )
        depth[node] = value
        return value

    for node in edges:
        resolve(node, frozenset())

    grouped: dict[int, list[str]] = {}
    for node, level in depth.items():
        grouped.setdefault(level, []).append(node)
    return [sorted(grouped[level]) for level in sorted(grouped)]


def item_states(
    edges: Mapping[str, Sequence[str]],
    run_statuses: Mapping[str, Sequence[RunStatus]],
) -> dict[str, WorkItemState]:
    """Derive item state from the graph and retained run attempts."""
    states: dict[str, WorkItemState] = {}

    def resolve(node: str, seen: frozenset[str]) -> WorkItemState:
        if node in states:
            return states[node]
        if node in seen:
            return WorkItemState.BLOCKED

        statuses = list(run_statuses.get(node, ()))
        if RunStatus.SUCCEEDED in statuses:
            state = WorkItemState.COMPLETED
        elif RunStatus.RUNNING in statuses:
            state = WorkItemState.RUNNING
        else:
            dependencies = [
                dependency for dependency in edges.get(node, ()) if dependency in edges
            ]
            satisfied = all(
                resolve(dependency, seen | {node}) is WorkItemState.COMPLETED
                for dependency in dependencies
            )
            if not satisfied:
                state = WorkItemState.BLOCKED
            elif statuses and all(status is RunStatus.FAILED for status in statuses):
                state = WorkItemState.FAILED
            else:
                state = WorkItemState.READY

        states[node] = state
        return state

    for node in edges:
        resolve(node, frozenset())
    return states


def blocked_by(
    node: str,
    edges: Mapping[str, Sequence[str]],
    states: Mapping[str, WorkItemState],
) -> list[str]:
    return sorted(
        dependency
        for dependency in edges.get(node, ())
        if states.get(dependency) is not WorkItemState.COMPLETED
    )


def parallel_candidates(node: str, edges: Mapping[str, Sequence[str]]) -> list[str]:
    """Return items with no dependency path to or from this item."""
    downstream = _reachable(node, edges)
    return sorted(
        other
        for other in edges
        if other != node
        and other not in downstream
        and node not in _reachable(other, edges)
    )


def _reachable(node: str, edges: Mapping[str, Sequence[str]]) -> set[str]:
    seen: set[str] = set()
    frontier = [node]
    while frontier:
        current = frontier.pop()
        for dependency in edges.get(current, ()):
            if dependency not in seen and dependency in edges:
                seen.add(dependency)
                frontier.append(dependency)
    return seen


def edges_from_items(items: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    return {
        str(item["key"]): [
            str(dependency) for dependency in (item.get("dependencies") or [])
        ]
        for item in items
    }
