from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from docker.errors import APIError, ContainerError, DockerException, NotFound
from fastapi import HTTPException, status

ResponseType = TypeVar("ResponseType")
DOCKER_DAEMON_UNAVAILABLE_DETAIL = "Docker daemon is unavailable"


@dataclass(frozen=True)
class PassThroughApiError:
    """Return Docker's own status code, or 502 when it reports none."""

    detail: str


@dataclass(frozen=True)
class ConflictApiError:
    """Map only Docker's 409 to 409; collapse every other status to 502."""

    conflict_detail: str
    other_detail: str = "Docker rejected the request"


ApiErrorPolicy = PassThroughApiError | ConflictApiError


@dataclass(frozen=True)
class DockerErrorPolicy:
    domain_errors: tuple[type[Exception], ...]
    api_error: ApiErrorPolicy
    container_error_detail: str | None = None


def docker_response(
    function: Callable[[], ResponseType],
    policy: DockerErrorPolicy,
) -> ResponseType:
    container_error_detail = policy.container_error_detail
    container_errors = (
        (ContainerError,) if container_error_detail is not None else ()
    )
    try:
        return function()
    except policy.domain_errors as error:
        raise HTTPException(error.status_code, error.detail) from error
    except NotFound as error:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Docker resource not found",
        ) from error
    # Empty tuples catch nothing; ContainerError falls through to DockerException (503).
    except container_errors as error:
        # The daemon is reachable here: a helper container ran and exited non-zero.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"{container_error_detail}: {error}",
        ) from error
    except APIError as error:
        response_status = getattr(getattr(error, "response", None), "status_code", 0)
        if isinstance(policy.api_error, PassThroughApiError):
            raise HTTPException(
                response_status or status.HTTP_502_BAD_GATEWAY,
                policy.api_error.detail,
            ) from error
        if response_status == status.HTTP_409_CONFLICT:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                policy.api_error.conflict_detail,
            ) from error
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            policy.api_error.other_detail,
        ) from error
    except DockerException as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            DOCKER_DAEMON_UNAVAILABLE_DETAIL,
        ) from error
