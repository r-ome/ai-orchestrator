from app.previews.models import (
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewEnvironmentSource,
    PreviewMode,
    PreviewRuntime,
    PreviewServiceType,
)
from app.previews.runtimes.native import _native_service_environment

CREDENTIALS = {"username": "preview", "password": "secret"}


def _config() -> PreviewConfiguration:
    return PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        runtime=PreviewRuntime.NEXTJS,
        image="node:22-bookworm-slim",
        start_command="npm run start",
        container_port=3000,
        services={
            "database": PreviewDependencyService(
                type=PreviewServiceType.MYSQL, image="mysql:8.4"
            )
        },
        environment={
            "DATABASE_URL": PreviewEnvironmentSource(from_service="database"),
            "API_KEY": PreviewEnvironmentSource(from_secret="api_key"),
        },
    )


def test_native_service_environment_maps_each_variable_to_its_source_service() -> None:
    """The preview-to-sandbox translation must carry from_service through."""
    config = _config()

    environment = _native_service_environment(
        config, config.services["database"], CREDENTIALS
    )

    assert environment == {
        "DATABASE_URL": "mysql://preview:secret@database:3306/atc_preview"
    }


def test_native_service_environment_falls_back_to_the_service_database_name() -> None:
    config = _config()

    environment = _native_service_environment(
        config, config.services["database"], CREDENTIALS, database_name=""
    )

    assert environment["DATABASE_URL"].endswith("/atc_preview")


def test_native_service_environment_prefers_an_explicit_database_name() -> None:
    """A shared-data schema name overrides the service's own database name."""
    config = _config()

    environment = _native_service_environment(
        config,
        config.services["database"],
        CREDENTIALS,
        database_name="sbx_shared_schema",
    )

    assert environment["DATABASE_URL"].endswith("/sbx_shared_schema")
    assert "atc_preview" not in environment["DATABASE_URL"]
