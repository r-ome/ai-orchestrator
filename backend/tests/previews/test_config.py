import pytest

from app.containers.config import get_preview_runtime_limits
from app.previews.config import get_preview_settings


def test_preview_settings_compose_environment_backed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settings factory must resolve limits from the environment.

    Composing a bare ``PreviewRuntimeLimits()`` here would silently ignore
    every ``PREVIEW_*`` limit variable in production while leaving the
    isolated limits tests green.
    """
    get_preview_settings.cache_clear()
    get_preview_runtime_limits.cache_clear()
    try:
        monkeypatch.setenv("PREVIEW_MEMORY", "7g")
        monkeypatch.setenv("PREVIEW_PREPARE_TIMEOUT_SECONDS", "45")
        monkeypatch.setenv("PREVIEW_SHARED_DATABASE_MEMORY", "5g")
        monkeypatch.setenv("PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS", "150")

        limits = get_preview_settings().limits

        assert limits.memory == "7g"
        assert limits.prepare_timeout_seconds == 45
        assert limits.shared_database_memory == "5g"
        assert limits.shared_database_max_connections == 150
    finally:
        get_preview_settings.cache_clear()
        get_preview_runtime_limits.cache_clear()
