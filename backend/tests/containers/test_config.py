import pytest

from app.containers.config import get_git_settings


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
