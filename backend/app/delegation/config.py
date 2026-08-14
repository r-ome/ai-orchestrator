import os
from dataclasses import dataclass
from functools import lru_cache

from app.agents.models import AgentProvider
from app.delegation.routing import ProviderModels, RoutingSettings
from app.delegation.verification import VerificationSettings


@dataclass(frozen=True)
class DelegatorSettings:
    model: str


@dataclass(frozen=True)
class IntegrationReviewSettings:
    model: str


@dataclass(frozen=True)
class DriverSettings:
    """The unattended driver's only budget.

    A coding turn already caps itself at CODING_TURN_TIMEOUT_SECONDS, but a
    delegation runs its items in sequence, so the total has no ceiling of its
    own. This is the one the person who walked away actually cares about.
    """

    max_seconds: int


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


@lru_cache
def get_routing_settings() -> RoutingSettings:
    claude_default = os.getenv("ROUTING_CLAUDE_DEFAULT_MODEL", "claude-sonnet-5")
    codex_default = os.getenv("ROUTING_CODEX_DEFAULT_MODEL", "gpt-5.6-terra")
    return RoutingSettings(
        claude=ProviderModels(
            # ROUTING_LOW/MEDIUM/HIGH_MODEL named Claude models but applied to
            # both providers, so choosing Codex asked it for a Claude model.
            # They are read here as the Claude fallback, so an existing
            # deployment keeps its configured tiers.
            low_model=os.getenv(
                "ROUTING_CLAUDE_LOW_MODEL",
                os.getenv("ROUTING_LOW_MODEL", "claude-haiku-4-5-20251001"),
            ),
            medium_model=os.getenv(
                "ROUTING_CLAUDE_MEDIUM_MODEL",
                os.getenv("ROUTING_MEDIUM_MODEL", "claude-sonnet-5"),
            ),
            high_model=os.getenv(
                "ROUTING_CLAUDE_HIGH_MODEL",
                os.getenv("ROUTING_HIGH_MODEL", "claude-opus-5"),
            ),
            default_model=claude_default,
            catalogue=_catalogue("ROUTING_CLAUDE_MODELS", DEFAULT_CLAUDE_MODELS),
        ),
        codex=ProviderModels(
            low_model=os.getenv("ROUTING_CODEX_LOW_MODEL", "gpt-5.6-luna"),
            medium_model=os.getenv("ROUTING_CODEX_MEDIUM_MODEL", "gpt-5.6-terra"),
            high_model=os.getenv("ROUTING_CODEX_HIGH_MODEL", "gpt-5.6-sol"),
            default_model=codex_default,
            catalogue=_catalogue("ROUTING_CODEX_MODELS", DEFAULT_CODEX_MODELS),
        ),
        default_provider=_provider("ROUTING_DEFAULT_PROVIDER", AgentProvider.CLAUDE),
    )


def _provider(variable: str, fallback: AgentProvider) -> AgentProvider:
    """Read a provider name, falling back when it names nothing we serve."""
    try:
        return AgentProvider(os.getenv(variable, fallback.value))
    except ValueError:
        return fallback


def _catalogue(variable: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Read a comma-separated model list, keeping order and dropping blanks."""
    raw = os.getenv(variable)
    if raw is None:
        return fallback
    models = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    return models or fallback


@lru_cache
def get_verification_settings() -> VerificationSettings:
    return VerificationSettings(
        image=os.getenv(
            "DELEGATION_VERIFICATION_IMAGE",
            os.getenv("TASK_GIT_IMAGE", "orchestrator-agent-claude:latest"),
        ),
        timeout_seconds=_positive_integer(
            os.getenv("DELEGATION_VERIFICATION_TIMEOUT_SECONDS"),
            600,
        ),
        memory=os.getenv("DELEGATION_VERIFICATION_MEMORY", "2g"),
        pids_limit=_positive_integer(
            os.getenv("DELEGATION_VERIFICATION_PIDS_LIMIT"),
            512,
        ),
        max_output_bytes=_positive_integer(
            os.getenv("DELEGATION_VERIFICATION_MAX_OUTPUT_BYTES"),
            100_000,
        ),
    )


@lru_cache
def get_delegator_settings() -> DelegatorSettings:
    return DelegatorSettings(
        model=os.getenv("DELEGATOR_MODEL", "claude-sonnet-5"),
    )


@lru_cache
def get_driver_settings() -> DriverSettings:
    return DriverSettings(
        max_seconds=_positive_integer(
            os.getenv("DELEGATION_DRIVER_MAX_SECONDS"),
            7200,
        ),
    )


@lru_cache
def get_integration_review_settings() -> IntegrationReviewSettings:
    return IntegrationReviewSettings(
        model=os.getenv("INTEGRATION_REVIEW_MODEL", "claude-sonnet-5"),
    )


def _positive_integer(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
