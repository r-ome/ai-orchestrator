import os
from dataclasses import dataclass
from functools import lru_cache

# alpine:latest has no git, and git containers run with denied egress,
# so git cannot be installed at runtime.
DEFAULT_GIT_IMAGE = "alpine/git:latest"


@dataclass(frozen=True)
class GitSettings:
    git_image: str = DEFAULT_GIT_IMAGE


@lru_cache
def get_git_settings() -> GitSettings:
    return GitSettings(git_image=os.getenv("PREVIEW_GIT_IMAGE", DEFAULT_GIT_IMAGE))
