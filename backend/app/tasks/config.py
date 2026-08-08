import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class CodingTurnSettings:
    """Limits for one writable coding turn.

    Separate from PlanningSettings because a turn that writes code needs more
    room and more time than one that reads it, and because its tool allowance
    is the opposite shape.
    """

    timeout_seconds: int
    memory: str
    pids_limit: int
    max_log_bytes: int
    claude_model: str
    codex_model: str
    codex_reasoning_effort: str
    credential_profile: str

    def model(self, provider_value: str) -> str:
        return self.claude_model if provider_value == "claude" else self.codex_model


@lru_cache
def get_coding_turn_settings() -> CodingTurnSettings:
    return CodingTurnSettings(
        timeout_seconds=_positive_integer(
            os.getenv("CODING_TURN_TIMEOUT_SECONDS"),
            default=1800,
        ),
        memory=os.getenv("CODING_TURN_MEMORY", "4g"),
        pids_limit=_positive_integer(os.getenv("CODING_TURN_PIDS_LIMIT"), default=512),
        max_log_bytes=_positive_integer(
            os.getenv("CODING_TURN_MAX_LOG_BYTES"),
            default=2_000_000,
        ),
        claude_model=os.getenv("CODING_TURN_CLAUDE_MODEL", "claude-sonnet-5"),
        codex_model=os.getenv("CODING_TURN_CODEX_MODEL", "gpt-5.6-terra"),
        codex_reasoning_effort=os.getenv(
            "CODING_TURN_CODEX_REASONING_EFFORT",
            "medium",
        ),
        credential_profile=os.getenv("CODING_TURN_CREDENTIAL_PROFILE", "default"),
    )


def _positive_integer(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
