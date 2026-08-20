"""Which models each agent provider serves.

The catalogue answers two questions: which provider serves a given model, and
what an operator may choose. Both are agents questions, so planning asks them
here instead of reaching into `delegation.config`, which put
`planning -> delegation` into the app import cycle. Delegation's routing
settings build their per-provider catalogues from this same source.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

from app.agents.models import AgentProvider

# Each provider serves its own models, so the catalogues never overlap. Order
# is best first; it is the order the model override dropdown shows.
DEFAULT_CLAUDE_MODELS = (
    "claude-fable-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
)
DEFAULT_CODEX_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
)


@dataclass(frozen=True)
class ModelCatalogue:
    claude: tuple[str, ...]
    codex: tuple[str, ...]

    def for_provider(self, provider: AgentProvider) -> tuple[str, ...]:
        return self.codex if provider is AgentProvider.CODEX else self.claude

    def as_dict(self) -> dict[str, tuple[str, ...]]:
        return {
            AgentProvider.CLAUDE.value: self.claude,
            AgentProvider.CODEX.value: self.codex,
        }

    def provider_for_model(self, model: str) -> AgentProvider | None:
        """Which provider serves this model, or None when nothing lists it."""
        if model in self.claude:
            return AgentProvider.CLAUDE
        if model in self.codex:
            return AgentProvider.CODEX
        return None


@lru_cache
def get_model_catalogue() -> ModelCatalogue:
    return ModelCatalogue(
        claude=_catalogue("ROUTING_CLAUDE_MODELS", DEFAULT_CLAUDE_MODELS),
        codex=_catalogue("ROUTING_CODEX_MODELS", DEFAULT_CODEX_MODELS),
    )


def _catalogue(variable: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated model list, keeping order and dropping blanks."""
    raw = os.getenv(variable)
    if raw is None:
        return fallback
    models = tuple(
        dict.fromkeys(part.strip() for part in raw.split(",") if part.strip())
    )
    return models or fallback
