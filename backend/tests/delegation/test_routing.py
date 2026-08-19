import pytest

from app.agents.models import AgentProvider
from app.delegation.models import Complexity
from app.delegation.routing import (
    ProviderModels,
    RoutingSettings,
    RoutingSource,
    route,
)

CLAUDE = ProviderModels(
    low_model="cheap",
    medium_model="standard",
    high_model="strong",
    default_model="standard",
    catalogue=("strong", "standard", "cheap"),
)
CODEX = ProviderModels(
    low_model="gpt-cheap",
    medium_model="gpt-standard",
    high_model="gpt-strong",
    default_model="gpt-standard",
    catalogue=("gpt-strong", "gpt-standard", "gpt-cheap"),
)
SETTINGS = RoutingSettings(claude=CLAUDE, codex=CODEX)


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


@pytest.mark.parametrize(
    ("complexity", "expected"),
    [
        (Complexity.LOW, "gpt-cheap"),
        (Complexity.MEDIUM, "gpt-standard"),
        (Complexity.HIGH, "gpt-strong"),
    ],
)
def test_codex_recommends_its_own_models(
    complexity: Complexity,
    expected: str,
) -> None:
    """The reported defect: choosing Codex used to recommend a Claude model."""
    decision = route(complexity, SETTINGS, item_provider=AgentProvider.CODEX)

    assert decision.provider is AgentProvider.CODEX
    assert decision.model == expected
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


def test_configured_default_provider_applies_with_no_preference() -> None:
    """A deployment that implements on Codex should not need a per-run choice."""
    settings = RoutingSettings(
        claude=CLAUDE,
        codex=CODEX,
        default_provider=AgentProvider.CODEX,
    )

    decision = route(Complexity.MEDIUM, settings)

    assert decision.provider is AgentProvider.CODEX
    assert decision.model == "gpt-standard"


def test_a_run_preference_still_beats_the_default_provider() -> None:
    settings = RoutingSettings(
        claude=CLAUDE,
        codex=CODEX,
        default_provider=AgentProvider.CODEX,
    )

    decision = route(
        Complexity.MEDIUM,
        settings,
        run_provider=AgentProvider.CLAUDE,
    )

    assert decision.provider is AgentProvider.CLAUDE
    assert decision.model == "standard"


def test_identical_tiers_disable_warning() -> None:
    one = ProviderModels("one", "one", "one", "one", ("one",))
    flat = RoutingSettings(claude=one, codex=one)

    assert route(Complexity.HIGH, flat, item_model="one").warning is None


def test_a_model_the_other_provider_owns_is_refused_and_explained() -> None:
    """Sending a Claude model name to `codex exec` fails, so never do it."""
    decision = route(
        Complexity.MEDIUM,
        SETTINGS,
        item_provider=AgentProvider.CODEX,
        item_model="strong",
    )

    assert decision.model == "gpt-standard"
    assert decision.source is RoutingSource.COMPLEXITY
    assert "is a claude model and codex cannot run it" in (decision.warning or "")


def test_a_run_preference_for_the_other_provider_is_refused() -> None:
    decision = route(
        Complexity.LOW,
        SETTINGS,
        item_provider=AgentProvider.CODEX,
        run_model="cheap",
    )

    assert decision.model == "gpt-cheap"
    assert decision.source is RoutingSource.COMPLEXITY


def test_an_unlisted_model_is_still_allowed_through() -> None:
    """A model newer than this deployment knows about must stay reachable."""
    decision = route(
        Complexity.LOW,
        SETTINGS,
        item_provider=AgentProvider.CODEX,
        item_model="gpt-6-unreleased",
    )

    assert decision.model == "gpt-6-unreleased"
    assert decision.source is RoutingSource.ITEM_OVERRIDE
    assert decision.warning is None


def test_an_item_model_is_not_replaced_by_a_run_preference() -> None:
    """An item override that is refused falls to complexity, not to the run."""
    decision = route(
        Complexity.HIGH,
        SETTINGS,
        item_provider=AgentProvider.CODEX,
        item_model="strong",
        run_model="gpt-cheap",
    )

    assert decision.model == "gpt-strong"
    assert decision.source is RoutingSource.COMPLEXITY


def test_provider_for_model_reads_both_catalogues() -> None:
    assert SETTINGS.provider_for_model("cheap") is AgentProvider.CLAUDE
    assert SETTINGS.provider_for_model("gpt-strong") is AgentProvider.CODEX
    assert SETTINGS.provider_for_model("unlisted") is None
