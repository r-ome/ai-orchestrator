import os
from dataclasses import dataclass
from functools import lru_cache

from app.delegation.routing import RoutingSettings
from app.delegation.verification import VerificationSettings


@dataclass(frozen=True)
class DelegatorSettings:
    model: str


@dataclass(frozen=True)
class IntegrationReviewSettings:
    model: str


@lru_cache
def get_routing_settings() -> RoutingSettings:
    default = os.getenv("ROUTING_DEFAULT_MODEL", "claude-sonnet-5")
    return RoutingSettings(
        low_model=os.getenv("ROUTING_LOW_MODEL", "claude-haiku-4-5-20251001"),
        medium_model=os.getenv("ROUTING_MEDIUM_MODEL", "claude-sonnet-5"),
        high_model=os.getenv("ROUTING_HIGH_MODEL", "claude-opus-5"),
        default_model=default,
    )


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
