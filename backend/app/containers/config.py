import os
from dataclasses import dataclass
from functools import lru_cache

from app.platform.env import integer_setting

# alpine:latest has no git, and git containers run with denied egress,
# so git cannot be installed at runtime.
DEFAULT_GIT_IMAGE = "alpine/git:latest"


@dataclass(frozen=True)
class GitSettings:
    git_image: str = DEFAULT_GIT_IMAGE


@lru_cache
def get_git_settings() -> GitSettings:
    return GitSettings(git_image=os.getenv("PREVIEW_GIT_IMAGE", DEFAULT_GIT_IMAGE))


DEFAULT_PREVIEW_MEMORY = "4g"
DEFAULT_PREVIEW_PREPARE_TIMEOUT_SECONDS = 600
DEFAULT_PREVIEW_SHARED_DATABASE_MEMORY = "2g"
DEFAULT_PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS = 200


@dataclass(frozen=True)
class PreviewRuntimeLimits:
    memory: str = DEFAULT_PREVIEW_MEMORY
    prepare_timeout_seconds: int = DEFAULT_PREVIEW_PREPARE_TIMEOUT_SECONDS
    shared_database_memory: str = DEFAULT_PREVIEW_SHARED_DATABASE_MEMORY
    shared_database_max_connections: int = (
        DEFAULT_PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS
    )


@lru_cache
def get_preview_runtime_limits() -> PreviewRuntimeLimits:
    return PreviewRuntimeLimits(
        memory=os.getenv("PREVIEW_MEMORY", DEFAULT_PREVIEW_MEMORY),
        prepare_timeout_seconds=integer_setting(
            "PREVIEW_PREPARE_TIMEOUT_SECONDS",
            DEFAULT_PREVIEW_PREPARE_TIMEOUT_SECONDS,
        ),
        shared_database_memory=os.getenv(
            "PREVIEW_SHARED_DATABASE_MEMORY",
            DEFAULT_PREVIEW_SHARED_DATABASE_MEMORY,
        ),
        shared_database_max_connections=integer_setting(
            "PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS",
            DEFAULT_PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS,
        ),
    )
