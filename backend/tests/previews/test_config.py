import pytest

from app.containers.config import get_preview_runtime_limits
from app.previews.config import (
    DEFAULT_PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES,
    DEFAULT_PREVIEW_MAXIMUM_DEPENDENCY_BYTES,
    PreviewSettings,
    get_preview_settings,
)


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


def test_preview_settings_read_the_size_limits_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both size caps must be environment-backed.

    They cap a built image at ``resources.py:85`` and a dependency install at
    ``runtimes/native.py:592``. Misspelling either variable name would make
    production ignore the operator's limit and keep the suite green.
    """
    get_preview_settings.cache_clear()
    try:
        monkeypatch.setenv("PREVIEW_MAXIMUM_DEPENDENCY_BYTES", "111")
        monkeypatch.setenv("PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES", "222")

        settings = get_preview_settings()

        assert settings.maximum_dependency_bytes == 111
        assert settings.maximum_built_image_bytes == 222
    finally:
        get_preview_settings.cache_clear()


def test_preview_settings_size_limits_fall_back_to_their_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_preview_settings.cache_clear()
    try:
        monkeypatch.delenv("PREVIEW_MAXIMUM_DEPENDENCY_BYTES", raising=False)
        monkeypatch.delenv("PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES", raising=False)

        settings = get_preview_settings()

        assert settings.maximum_dependency_bytes == (
            DEFAULT_PREVIEW_MAXIMUM_DEPENDENCY_BYTES
        )
        assert settings.maximum_built_image_bytes == (
            DEFAULT_PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES
        )
    finally:
        get_preview_settings.cache_clear()


def test_preview_settings_size_limit_fields_default_to_their_constants() -> None:
    """Ten direct constructions omit both size caps and inherit these defaults.

    ``get_preview_settings`` always passes both fields, so the factory never
    exercises them. Only a direct construction does, and that is the path every
    preview test takes.
    """
    settings = PreviewSettings(
        inspection_image="alpine:latest",
        default_expiry_minutes=30,
        maximum_file_bytes=1,
        maximum_snapshot_bytes=1,
        proposal_lifetime_seconds=1,
        build_timeout_seconds=1,
    )

    assert settings.maximum_dependency_bytes == DEFAULT_PREVIEW_MAXIMUM_DEPENDENCY_BYTES
    assert (
        settings.maximum_built_image_bytes == DEFAULT_PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES
    )
