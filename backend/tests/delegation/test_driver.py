from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.delegation import driver, execution
from app.delegation.config import DriverSettings
from app.delegation.models import (
    DelegationStatus,
    RunStatus,
    StartRunRequest,
    WorkItemState,
)
from app.delegation.routing import ProviderModels, RoutingSettings
from tests.delegation.test_execution import (
    SETTINGS,
    _delegation,
    _item,
    _Tasks,
    store,  # noqa: F401 - imported so pytest resolves the fixture here
    tasks,  # noqa: F401 - same
)

CAP = DriverSettings(max_seconds=7200)
CODEX_FIRST = RoutingSettings(
    claude=ProviderModels(
        low_model="cheap",
        medium_model="standard",
        high_model="strong",
        default_model="standard",
        catalogue=("strong", "standard", "cheap"),
    ),
    codex=ProviderModels(
        low_model="gpt-cheap",
        medium_model="gpt-standard",
        high_model="gpt-strong",
        default_model="gpt-standard",
        catalogue=("gpt-strong", "gpt-standard", "gpt-cheap"),
    ),
    default_provider=AgentProvider.CODEX,
)


def _drive(
    store: ControllerStore,
    delegation_id: str,
    *,
    driver_settings: DriverSettings = CAP,
    routing_settings: RoutingSettings | None = None,
    clock: Callable[[], float] | None = None,
) -> driver.DriveOutcome:
    return driver.drive_delegation(
        object(),
        store,
        delegation_id,
        settings=SETTINGS,
        driver_settings=driver_settings,
        routing_settings=routing_settings,
        session_id="session-1",
        project_name="sample",
        clock=clock or (lambda: 0.0),
    )


def _clock(*values: float) -> Callable[[], float]:
    """A clock that reads each value once, then holds the last one."""
    remaining: Iterator[float] = iter(values)
    last = [values[0]]

    def read() -> float:
        try:
            last[0] = next(remaining)
        except StopIteration:
            pass
        return last[0]

    return read


def _fail_first_run(tasks: _Tasks, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the first coding turn commit nothing, so item one fails."""
    inner = tasks.run_task

    def run_task(*args: Any, **kwargs: Any) -> Any:
        tasks.committed = tasks.run_count >= 1
        return inner(*args, **kwargs)

    monkeypatch.setattr(execution, "run_task", run_task)


def test_driver_runs_a_whole_dependency_chain_without_a_person(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(
        store,
        [
            _item("a"),
            _item("b", dependencies=["a"]),
            _item("c", dependencies=["b"]),
        ],
    )

    outcome = _drive(store, delegation.delegation.id)

    assert outcome.attempted == ["a", "b", "c"]
    assert outcome.completed == ["a", "b", "c"]
    assert outcome.failed == []
    assert outcome.status is DelegationStatus.COMPLETED
    # Three items, three merges, no human accept anywhere in between.
    assert len(tasks.accepted) == 3


def test_driver_runs_earlier_waves_first(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    """Each item is cut from the branch the last one left, so order is not cosmetic."""
    delegation = _delegation(
        store,
        [
            _item("zebra"),
            _item("alpha", dependencies=["zebra"]),
        ],
    )

    outcome = _drive(store, delegation.delegation.id)

    # Alphabetical order would have run 'alpha' first, and it was blocked.
    assert outcome.attempted == ["zebra", "alpha"]


def test_a_failure_stops_its_dependents_but_not_an_independent_item(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_first_run(tasks, monkeypatch)
    delegation = _delegation(
        store,
        [
            _item("a"),
            _item("b"),
            _item("c", dependencies=["a"]),
        ],
    )

    outcome = _drive(store, delegation.delegation.id)

    assert outcome.failed == ["a"]
    # 'b' shares nothing with 'a', so the driver kept going.
    assert outcome.completed == ["b"]
    # 'c' depends on 'a'. The graph blocked it, so it was never offered.
    assert outcome.blocked == ["c"]
    assert "c" not in outcome.attempted
    assert outcome.status is DelegationStatus.HALTED
    assert "a" in outcome.stopped_because
    assert "c" in outcome.stopped_because


def test_driver_does_not_retry_a_failed_item(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claim_run` accepts a FAILED item, so only the READY filter prevents a retry loop."""
    _fail_first_run(tasks, monkeypatch)
    delegation = _delegation(store, [_item("a")])

    outcome = _drive(store, delegation.delegation.id)

    assert outcome.attempted == ["a"]
    assert outcome.failed == ["a"]
    assert tasks.run_count == 1


def test_wall_clock_cap_stops_before_starting_the_next_item(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    delegation = _delegation(store, [_item("a"), _item("b", dependencies=["a"])])

    outcome = _drive(
        store,
        delegation.delegation.id,
        driver_settings=DriverSettings(max_seconds=100),
        clock=_clock(0.0, 0.0, 500.0),
    )

    assert outcome.attempted == ["a"]
    assert outcome.completed == ["a"]
    assert outcome.status is DelegationStatus.HALTED
    assert "wall-clock cap of 100s" in outcome.stopped_because
    assert "'b' still to run" in outcome.stopped_because


def test_driver_stops_when_a_run_leaves_the_item_ready(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this guard a claim that records no run would spin forever."""
    monkeypatch.setattr(
        driver,
        "start_run",
        lambda *args, **kwargs: SimpleNamespace(run_status=RunStatus.SUCCEEDED),
    )
    delegation = _delegation(store, [_item("a")])

    outcome = _drive(store, delegation.delegation.id)

    assert outcome.attempted == ["a"]
    assert "did not leave the ready state" in outcome.stopped_because
    # Nothing ran, so the delegation is still READY and still re-drivable.
    # READY has no transition to HALTED, and inventing one would report work
    # that never started as abandoned work.
    assert outcome.status is DelegationStatus.READY


def test_driver_does_not_start_work_on_a_halted_delegation(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    from app.delegation import service

    delegation = _delegation(store, [_item("a")])
    service.transition(
        store,
        delegation.delegation.id,
        DelegationStatus.RUNNING,
        session_id="session-1",
        project_name="sample",
    )
    service.transition(
        store,
        delegation.delegation.id,
        DelegationStatus.HALTED,
        error="stopped by a person",
        session_id="session-1",
        project_name="sample",
    )

    outcome = _drive(store, delegation.delegation.id)

    assert outcome.attempted == []
    assert tasks.run_count == 0
    assert "halted" in outcome.stopped_because


def test_driver_leaves_provider_choice_to_the_routing_default(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    """The driver expresses no run preference, so ROUTING_DEFAULT_PROVIDER decides."""
    delegation = _delegation(store, [_item("a")])

    _drive(store, delegation.delegation.id, routing_settings=CODEX_FIRST)

    view = _view(store, delegation.delegation.id)
    assert view.items[0].runs[0].provider is AgentProvider.CODEX
    assert view.items[0].runs[0].model == "gpt-cheap"


def test_drive_progress_is_streamable_and_ends_on_a_terminal_step(
    store: ControllerStore,
    tasks: _Tasks,
) -> None:
    """The console closes on a terminal step, so the drive has to record one."""
    from app.turns.locators import EVENT_KINDS, TERMINAL_STEPS

    delegation = _delegation(store, [_item("a"), _item("b", dependencies=["a"])])

    _drive(store, delegation.delegation.id)

    events = store.events_for_run(
        delegation.delegation.id,
        kind=EVENT_KINDS["drive"],
    )
    steps = [event["payload"]["step"] for event in events]
    assert steps[0] == "started"
    assert steps.count("item_started") == 2
    assert steps[-1] in TERMINAL_STEPS


def test_absent_run_provider_is_not_an_override() -> None:
    """The default used to be CLAUDE, which made every silent request an override."""
    assert StartRunRequest().provider is None


def _view(store: ControllerStore, delegation_id: str) -> Any:
    from app.delegation import service

    return service.view(
        store,
        delegation_id,
        session_id="session-1",
        project_name="sample",
    )


def test_completed_items_are_reported_even_when_the_graph_stalls(
    store: ControllerStore,
    tasks: _Tasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_first_run(tasks, monkeypatch)
    delegation = _delegation(store, [_item("a"), _item("b"), _item("c")])

    outcome = _drive(store, delegation.delegation.id)

    assert set(outcome.completed) == {"b", "c"}
    assert outcome.failed == ["a"]
    assert outcome.blocked == []
    states = {entry.item.key: entry.state for entry in _view(store, delegation.delegation.id).items}
    assert states["a"] is WorkItemState.FAILED
    assert states["b"] is WorkItemState.COMPLETED
