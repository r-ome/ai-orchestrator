from app.previews.errors import PreviewOperationError
from app.previews.models import PreviewConfiguration


def _secret_environment(
    config: PreviewConfiguration, secrets: dict[str, str]
) -> dict[str, str]:
    """Resolves from_secret entries against stored project secrets.

    Fails before any container starts, so a missing secret never surfaces as a
    runtime crash inside the preview.
    """
    environment: dict[str, str] = {}
    for variable, source in config.environment.items():
        if not source.from_secret:
            continue
        if source.from_secret not in secrets:
            raise PreviewOperationError(
                422,
                f"Preview secret {source.from_secret!r} is not configured",
            )
        environment[variable] = secrets[source.from_secret]
    return environment
