from typing import Any

import pytest

from app.delegation.graph import (
    blocked_by,
    find_cycle,
    item_states,
    parallel_candidates,
    validate_work_items,
    waves,
)
from app.delegation.models import RunStatus, WorkItemState

KINDS = frozenset({"test", "lint", "build"})


def _item(key: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key,
        "title": f"Item {key}",
        "objective": "do the thing",
        "scope": "just the thing",
        "dependencies": [],
        "acceptance_criteria": ["the thing is done"],
        "verification": [{"command_kind": "test", "reason": "covered"}],
        "complexity": "medium",
    }
    item.update(overrides)
    return item


def test_well_formed_decomposition_validates() -> None:
    items = [_item("a"), _item("b", dependencies=["a"])]

    assert validate_work_items(items, available_command_kinds=KINDS) == []


def test_empty_and_oversized_decompositions_are_rejected() -> None:
    assert validate_work_items([]) != []
    assert validate_work_items([_item(str(index)) for index in range(61)]) != []


def test_non_object_item_does_not_break_validation_of_later_items() -> None:
    errors = validate_work_items(
        ["not-an-object", _item("a")],  # type: ignore[list-item]
        available_command_kinds=KINDS,
    )

    assert any("items[0] must be an object" in error for error in errors)


def test_duplicate_keys_are_rejected() -> None:
    errors = validate_work_items(
        [_item("a"), _item("a")],
        available_command_kinds=KINDS,
    )

    assert any("used more than once" in error for error in errors)


def test_unresolved_self_and_cyclic_dependencies_are_rejected() -> None:
    unresolved = validate_work_items(
        [_item("a", dependencies=["ghost"])],
        available_command_kinds=KINDS,
    )
    self_dependency = validate_work_items(
        [_item("a", dependencies=["a"])],
        available_command_kinds=KINDS,
    )
    cyclic = validate_work_items(
        [
            _item("a", dependencies=["c"]),
            _item("b", dependencies=["a"]),
            _item("c", dependencies=["b"]),
        ],
        available_command_kinds=KINDS,
    )

    assert any("not a work item" in error for error in unresolved)
    assert any("depends on itself" in error for error in self_dependency)
    assert any("form a cycle" in error and "->" in error for error in cyclic)


@pytest.mark.parametrize("field", ["title", "objective", "scope"])
def test_required_text_is_enforced(field: str) -> None:
    assert validate_work_items(
        [_item("a", **{field: ""})],
        available_command_kinds=KINDS,
    )


def test_acceptance_verification_and_complexity_are_enforced() -> None:
    assert validate_work_items(
        [_item("a", acceptance_criteria=[])],
        available_command_kinds=KINDS,
    )
    assert validate_work_items(
        [_item("a", verification=[])],
        available_command_kinds=KINDS,
    )
    assert validate_work_items(
        [_item("a", complexity="trivial")],
        available_command_kinds=KINDS,
    )


def test_verification_must_use_a_confirmed_command_kind() -> None:
    errors = validate_work_items(
        [_item("a", verification=[{"command_kind": "e2e"}])],
        available_command_kinds=KINDS,
    )

    assert any("not confirmed" in error for error in errors)


def test_verification_is_not_checked_without_an_inventory() -> None:
    assert validate_work_items(
        [_item("a", verification=[{"command_kind": "e2e"}])]
    ) == []


def test_no_confirmed_commands_rejects_every_verification_kind() -> None:
    errors = validate_work_items(
        [_item("a", verification=[{"command_kind": "build"}])],
        available_command_kinds=frozenset(),
    )

    assert any("confirmed kinds are []" in error for error in errors)


def test_find_cycle_returns_empty_for_clean_graph() -> None:
    assert find_cycle({"a": [], "b": ["a"]}) == []


def test_waves_group_by_dependency_depth() -> None:
    edges = {"a": [], "b": [], "c": ["a"], "d": ["a", "b"], "e": ["c", "d"]}

    assert waves(edges) == [["a", "b"], ["c", "d"], ["e"]]
    assert waves({"a": []}) == [["a"]]


def test_item_states_follow_run_results_and_dependencies() -> None:
    edges = {"a": [], "b": ["a"]}

    blocked = item_states(edges, {})
    failed = item_states(edges, {"a": [RunStatus.FAILED]})
    running = item_states(
        edges,
        {"a": [RunStatus.FAILED, RunStatus.RUNNING]},
    )
    completed = item_states(
        edges,
        {"a": [RunStatus.FAILED, RunStatus.SUCCEEDED]},
    )

    assert blocked["a"] is WorkItemState.READY
    assert blocked["b"] is WorkItemState.BLOCKED
    assert failed["a"] is WorkItemState.FAILED
    assert failed["b"] is WorkItemState.BLOCKED
    assert running["a"] is WorkItemState.RUNNING
    assert completed["a"] is WorkItemState.COMPLETED
    assert completed["b"] is WorkItemState.READY


def test_blocked_by_names_only_incomplete_dependencies() -> None:
    edges = {"a": [], "b": [], "c": ["a", "b"]}
    states = item_states(edges, {"a": [RunStatus.SUCCEEDED]})

    assert blocked_by("c", edges, states) == ["b"]


def test_parallel_candidates_have_no_ordering_path() -> None:
    edges = {"a": [], "b": [], "c": ["a"]}

    assert parallel_candidates("a", edges) == ["b"]
    assert parallel_candidates("c", edges) == ["b"]
    assert parallel_candidates("b", edges) == ["a", "c"]


def test_parallel_candidates_do_not_depend_on_progress_or_write_overlap() -> None:
    edges = {"schema": [], "page-one": ["schema"], "page-two": ["schema"]}

    assert item_states(edges, {})["page-one"] is WorkItemState.BLOCKED
    assert parallel_candidates("page-one", edges) == ["page-two"]
    assert parallel_candidates("a", {"a": [], "b": []}) == ["b"]


def test_transitive_dependants_are_not_parallel_candidates() -> None:
    edges = {"a": [], "b": ["a"], "c": ["b"]}

    assert parallel_candidates("a", edges) == []
    assert parallel_candidates("c", edges) == []
