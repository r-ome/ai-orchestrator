import pytest

from app.agents.models import AgentProvider
from app.delegation.models import Complexity
from app.delegation.routing import RoutingSettings, RoutingSource, route


SETTINGS = RoutingSettings(
    low_model="cheap",
    medium_model="standard",
    high_model="strong",
    default_model="standard",
)


@pytest.mark.parametrize(
    ("complexity", "expected"),
    [
        (Complexity.LOW, "cheap"),
        (Complexity.MEDIUM, "standard"),
        (Complexity.HIGH, "strong"),
    ],
)
def test_complexity_selects_configured_model(
    complexity: Complexity,
    expected: str,
) -> None:
    decision = route(complexity, SETTINGS)

    assert decision.model == expected
    assert decision.source is RoutingSource.COMPLEXITY
    assert decision.recommended_model == expected


def test_precedence_is_item_then_run_then_complexity() -> None:
    run_choice = route(Complexity.LOW, SETTINGS, run_model="strong")
    item_choice = route(
        Complexity.MEDIUM,
        SETTINGS,
        item_model="cheap",
        run_model="strong",
    )

    assert run_choice.source is RoutingSource.RUN_PREFERENCE
    assert run_choice.model == "strong"
    assert item_choice.source is RoutingSource.ITEM_OVERRIDE
    assert item_choice.model == "cheap"


def test_weak_known_model_warns_but_is_respected() -> None:
    decision = route(Complexity.HIGH, SETTINGS, item_model="cheap")

    assert decision.model == "cheap"
    assert "below the model" in (decision.warning or "")


def test_unknown_or_stronger_model_does_not_warn() -> None:
    assert route(Complexity.LOW, SETTINGS, item_model="strong").warning is None
    assert route(Complexity.HIGH, SETTINGS, item_model="other").warning is None


def test_item_provider_override_wins() -> None:
    decision = route(
        Complexity.LOW,
        SETTINGS,
        item_provider=AgentProvider.CODEX,
        run_provider=AgentProvider.CLAUDE,
    )

    assert decision.provider is AgentProvider.CODEX


def test_identical_tiers_disable_warning() -> None:
    flat = RoutingSettings("one", "one", "one", "one")

    assert route(Complexity.HIGH, flat, item_model="one").warning is None
