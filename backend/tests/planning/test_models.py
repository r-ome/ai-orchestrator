from app.planning.models import (
    PLANNING_TRANSITIONS,
    TERMINAL_PLANNING_STATUSES,
    PlanningStatus,
    source_statuses,
)


def test_every_status_has_a_transition_table_entry() -> None:
    assert set(PLANNING_TRANSITIONS) == set(PlanningStatus)


def test_every_non_terminal_status_has_an_exit() -> None:
    for status in set(PlanningStatus) - TERMINAL_PLANNING_STATUSES:
        assert PLANNING_TRANSITIONS[status]


def test_terminal_statuses_have_no_exit() -> None:
    for status in TERMINAL_PLANNING_STATUSES:
        assert PLANNING_TRANSITIONS[status] == frozenset()


def test_cancelled_is_reachable_from_every_non_terminal_status() -> None:
    for status in set(PlanningStatus) - TERMINAL_PLANNING_STATUSES:
        assert PlanningStatus.CANCELLED in PLANNING_TRANSITIONS[status]


def test_source_statuses_agrees_with_transition_table() -> None:
    for target in PlanningStatus:
        assert source_statuses(target) == frozenset(
            source
            for source, targets in PLANNING_TRANSITIONS.items()
            if target in targets
        )
