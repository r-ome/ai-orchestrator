import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


DEFAULT_CONTROLLER_DATA_DIRECTORY = Path(__file__).resolve().parents[2] / ".controller-data"
DEFAULT_GIT_SECRET_DIRECTORY = Path.home() / ".orchestrator" / "run-secrets"


@dataclass(frozen=True)
class ControllerSettings:
    data_directory: Path
    preview_expiry_seconds: int
    expiry_poll_seconds: int
    git_secret_directory: Path = DEFAULT_GIT_SECRET_DIRECTORY
    git_timeout_seconds: int = 600

    @property
    def database_path(self) -> Path:
        return self.data_directory / "controller.sqlite3"


@lru_cache
def get_controller_settings() -> ControllerSettings:
    data_directory = Path(
        os.environ.get("CONTROLLER_DATA_DIRECTORY", DEFAULT_CONTROLLER_DATA_DIRECTORY)
    ).expanduser()
    return ControllerSettings(
        data_directory=data_directory.resolve(),
        preview_expiry_seconds=_positive_integer(
            os.environ.get("PREVIEW_EXPIRY_SECONDS"),
            default=30 * 60,
        ),
        expiry_poll_seconds=_positive_integer(
            os.environ.get("PREVIEW_EXPIRY_POLL_SECONDS"),
            default=15,
        ),
        git_secret_directory=Path(
            os.environ.get(
                "CONTROLLER_GIT_SECRET_DIRECTORY",
                DEFAULT_GIT_SECRET_DIRECTORY,
            )
        )
        .expanduser()
        .resolve(),
        git_timeout_seconds=_positive_integer(
            os.environ.get("CONTROLLER_GIT_TIMEOUT_SECONDS"),
            default=600,
        ),
    )


def _positive_integer(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value or default)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
