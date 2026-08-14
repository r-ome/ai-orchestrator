import os
from dataclasses import dataclass
from functools import lru_cache

from app.agents.models import AgentProvider


@dataclass(frozen=True)
class PlanningSettings:
    clarifier_provider: AgentProvider
    planner_provider: AgentProvider
    reviewer_provider: AgentProvider
    credential_profile: str
    max_review_turns: int
    turn_timeout_seconds: int
    planning_memory: str
    claude_model: str
    codex_model: str
    codex_reasoning_effort: str
    turn_retries: int = 2
    turn_retry_backoff_seconds: int = 5


#: The reasoning efforts the planning dialog offers for a codex reviewer.
REASONING_EFFORTS = ("low", "medium", "high")


def reasoning_effort_choices(settings: PlanningSettings) -> list[str]:
    """The three standard efforts, plus this deployment's own if it differs.

    A provider may accept efforts outside the three. Including the configured
    one keeps the dialog able to show what is already in force, and keeps a
    round trip through the dialog from being refused.
    """
    if settings.codex_reasoning_effort in REASONING_EFFORTS:
        return list(REASONING_EFFORTS)
    return [*REASONING_EFFORTS, settings.codex_reasoning_effort]


@lru_cache
def get_planning_settings() -> PlanningSettings:
    return PlanningSettings(
        clarifier_provider=_provider("PLANNING_CLARIFIER_PROVIDER", AgentProvider.CLAUDE),
        planner_provider=_provider("PLANNING_PLANNER_PROVIDER", AgentProvider.CLAUDE),
        reviewer_provider=_provider("PLANNING_REVIEWER_PROVIDER", AgentProvider.CODEX),
        credential_profile=os.getenv("PLANNING_CREDENTIAL_PROFILE", "default"),
        max_review_turns=_integer("PLANNING_MAX_REVIEW_TURNS", 3),
        turn_timeout_seconds=_integer("PLANNING_TURN_TIMEOUT_SECONDS", 600),
        turn_retries=_integer("PLANNING_TURN_RETRIES", 2),
        turn_retry_backoff_seconds=_integer("PLANNING_TURN_RETRY_BACKOFF_SECONDS", 5),
        planning_memory=os.getenv("PLANNING_MEMORY", "2g"),
        claude_model=os.getenv("PLANNING_CLAUDE_MODEL", "opus"),
        codex_model=os.getenv("PLANNING_CODEX_MODEL", "gpt-5.6-terra"),
        codex_reasoning_effort=os.getenv("PLANNING_CODEX_REASONING_EFFORT", "high"),
    )


def _provider(name: str, default: AgentProvider) -> AgentProvider:
    try:
        return AgentProvider(os.getenv(name, default.value))
    except ValueError:
        return default


def _integer(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default
