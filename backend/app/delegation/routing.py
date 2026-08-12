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
class ProviderModels:
    """One provider's complexity tiers and the models it will accept.

    Tiers are per provider because a model name only means anything to the
    provider that serves it. Sending a Claude model name to `codex exec`
    fails; the two catalogues never overlap.
    """

    low_model: str
    medium_model: str
    high_model: str
    default_model: str
    #: Every model the operator may choose for this provider, best first.
    catalogue: tuple[str, ...]

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
class RoutingSettings:
    claude: ProviderModels
    codex: ProviderModels

    def for_provider(self, provider: AgentProvider) -> ProviderModels:
        return self.codex if provider is AgentProvider.CODEX else self.claude

    def catalogue(self) -> dict[str, tuple[str, ...]]:
        return {
            AgentProvider.CLAUDE.value: self.claude.catalogue,
            AgentProvider.CODEX.value: self.codex.catalogue,
        }

    def provider_for_model(self, model: str) -> AgentProvider | None:
        """Which provider serves this model, or None when nothing lists it."""
        if model in self.claude.catalogue:
            return AgentProvider.CLAUDE
        if model in self.codex.catalogue:
            return AgentProvider.CODEX
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
    """Apply override, run preference, complexity, then default precedence.

    The provider is resolved first, because it selects the catalogue every
    later step reads. A model chosen for one provider is never carried over to
    another: the name would be meaningless to it.
    """
    provider = item_provider or run_provider or AgentProvider.CLAUDE
    models = settings.for_provider(provider)
    recommended = models.for_complexity(complexity)

    if item_model and _serves(settings, provider, item_model):
        model, source = item_model, RoutingSource.ITEM_OVERRIDE
    elif not item_model and run_model and _serves(settings, provider, run_model):
        model, source = run_model, RoutingSource.RUN_PREFERENCE
    elif recommended:
        model, source = recommended, RoutingSource.COMPLEXITY
    else:
        model, source = models.default_model, RoutingSource.DEFAULT

    return RoutingDecision(
        model=model,
        provider=provider,
        source=source,
        recommended_model=recommended,
        warning=_warning(complexity, model, recommended, models)
        or _mismatch(settings, provider, item_model or run_model, model),
    )


def _serves(
    settings: RoutingSettings,
    provider: AgentProvider,
    model: str,
) -> bool:
    """Whether this provider serves this model.

    A model no catalogue lists is allowed through, so an operator can reach a
    model newer than this deployment knows about. Only a model that another
    provider demonstrably owns is rejected.
    """
    owner = settings.provider_for_model(model)
    return owner is None or owner is provider


def _mismatch(
    settings: RoutingSettings,
    provider: AgentProvider,
    requested: str | None,
    model: str,
) -> str | None:
    if not requested or requested == model:
        return None
    owner = settings.provider_for_model(requested)
    if owner is None or owner is provider:
        return None
    return (
        f"'{requested}' is a {owner.value} model and {provider.value} cannot "
        f"run it. Using '{model}' instead."
    )


def _warning(
    complexity: Complexity,
    model: str,
    recommended: str,
    models: ProviderModels,
) -> str | None:
    if model == recommended:
        return None
    chosen = models.tier(model)
    wanted = models.tier(recommended)
    if chosen is None or wanted is None or chosen >= wanted:
        return None
    return (
        f"'{model}' is below the model this project uses for "
        f"{complexity.value}-complexity work ('{recommended}'). Running it anyway."
    )
