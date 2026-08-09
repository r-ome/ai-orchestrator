import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ContextSettings:
    model: str
    git_image: str
    inventory_timeout_seconds: int


@lru_cache
def get_context_settings() -> ContextSettings:
    return ContextSettings(
        # The alias, not a pinned id, so this tracks the provider's current top
        # model the way PLANNING_CLAUDE_MODEL does. Context generation reads the
        # whole repository once and everything downstream quotes it.
        model=os.getenv("CONTEXT_MODEL", "opus"),
        git_image=os.getenv("TASK_GIT_IMAGE", "orchestrator-agent-claude:latest"),
        inventory_timeout_seconds=_positive_integer(
            os.getenv("CONTEXT_INVENTORY_TIMEOUT_SECONDS"),
            default=60,
        ),
    )


def _positive_integer(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
