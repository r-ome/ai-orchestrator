import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROJECTS_ROOT = "/Users/jeromeagapay/Documents"
DEFAULT_COPY_IMAGE = "alpine:latest"


@dataclass(frozen=True)
class ProjectSettings:
    projects_root: Path
    copy_image: str


def get_project_settings() -> ProjectSettings:
    projects_root = Path(
        os.environ.get("PROJECTS_ROOT", DEFAULT_PROJECTS_ROOT)
    ).expanduser()
    return ProjectSettings(
        projects_root=projects_root.resolve(),
        copy_image=os.environ.get("PROJECT_COPY_IMAGE", DEFAULT_COPY_IMAGE),
    )
