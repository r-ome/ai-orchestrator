"""Choose the provider and model for one work item run."""

from dataclasses import dataclass
from enum import StrEnum

from app.agents.models import AgentProvider
from app.delegation.models import Complexity


class RoutingSource(StrEnum):
    ITEM_OVERRIDE = "item_override"
    RUN_PREFERENCE = "run_preference"
    COMPLEXITY = "complexity"
    DEFAULT = "default"


@dataclass(frozen=True)
class RoutingSettings:
    low_model: str
    medium_model: str
    high_model: str
    default_model: str

    def for_complexity(self, complexity: Complexity) -> str:
        return {
            Complexity.LOW: self.low_model,
            Complexity.MEDIUM: self.medium_model,
            Complexity.HIGH: self.high_model,
        }[complexity]

    def tier(self, model: str) -> int | None:
        for index, configured in enumerate(
            (self.low_model, self.medium_model, self.high_model)
        ):
            if configured == model:
                return index
        return None


@dataclass(frozen=True)
class RoutingDecision:
    model: str
    provider: AgentProvider
    source: RoutingSource
    recommended_model: str
    warning: str | None = None


def route(
    complexity: Complexity,
    settings: RoutingSettings,
    *,
    item_provider: AgentProvider | None = None,
    item_model: str | None = None,
    run_provider: AgentProvider | None = None,
    run_model: str | None = None,
) -> RoutingDecision:
    """Apply override, run preference, complexity, then default precedence."""
    recommended = settings.for_complexity(complexity)
    if item_model:
        model, source = item_model, RoutingSource.ITEM_OVERRIDE
    elif run_model:
        model, source = run_model, RoutingSource.RUN_PREFERENCE
    elif recommended:
        model, source = recommended, RoutingSource.COMPLEXITY
    else:
        model, source = settings.default_model, RoutingSource.DEFAULT

    provider = item_provider or run_provider or AgentProvider.CLAUDE
    return RoutingDecision(
        model=model,
        provider=provider,
        source=source,
        recommended_model=recommended,
        warning=_warning(complexity, model, recommended, settings),
    )


def _warning(
    complexity: Complexity,
    model: str,
    recommended: str,
    settings: RoutingSettings,
) -> str | None:
    if model == recommended:
        return None
    chosen = settings.tier(model)
    wanted = settings.tier(recommended)
    if chosen is None or wanted is None or chosen >= wanted:
        return None
    return (
        f"'{model}' is below the model this project uses for "
        f"{complexity.value}-complexity work ('{recommended}'). Running it anyway."
    )
