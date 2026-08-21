import pytest

from app.containers.config import get_git_settings, get_preview_runtime_limits


def test_git_settings_uses_the_default_image(monkeypatch: pytest.MonkeyPatch) -> None:
    get_git_settings.cache_clear()
    try:
        monkeypatch.delenv("PREVIEW_GIT_IMAGE", raising=False)
        assert get_git_settings().git_image == "alpine/git:latest"
    finally:
        get_git_settings.cache_clear()


def test_git_settings_reads_the_preview_git_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_git_settings.cache_clear()
    try:
        monkeypatch.setenv("PREVIEW_GIT_IMAGE", "example/git:1")
        assert get_git_settings().git_image == "example/git:1"
    finally:
        get_git_settings.cache_clear()


def test_preview_runtime_limits_use_code_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_preview_runtime_limits.cache_clear()
    try:
        monkeypatch.delenv("PREVIEW_MEMORY", raising=False)
        monkeypatch.delenv("PREVIEW_PREPARE_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("PREVIEW_SHARED_DATABASE_MEMORY", raising=False)
        monkeypatch.delenv("PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS", raising=False)

        limits = get_preview_runtime_limits()

        assert limits.memory == "4g"
        assert limits.prepare_timeout_seconds == 600
        assert limits.shared_database_memory == "2g"
        assert limits.shared_database_max_connections == 200
    finally:
        get_preview_runtime_limits.cache_clear()


def test_preview_runtime_limits_read_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_preview_runtime_limits.cache_clear()
    try:
        monkeypatch.setenv("PREVIEW_MEMORY", "8g")
        monkeypatch.setenv("PREVIEW_PREPARE_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("PREVIEW_SHARED_DATABASE_MEMORY", "3g")
        monkeypatch.setenv("PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS", "300")

        limits = get_preview_runtime_limits()

        assert limits.memory == "8g"
        assert limits.prepare_timeout_seconds == 120
        assert limits.shared_database_memory == "3g"
        assert limits.shared_database_max_connections == 300
    finally:
        get_preview_runtime_limits.cache_clear()
