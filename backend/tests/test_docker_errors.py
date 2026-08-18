import pytest
from docker.errors import APIError, ContainerError, DockerException, NotFound
from fastapi import HTTPException

from app.delegation.router import _DOCKER_ERRORS as DELEGATION_DOCKER_ERRORS
from app.docker_errors import (
    ConflictApiError,
    DockerErrorPolicy,
    PassThroughApiError,
    docker_response,
)
from app.implementation_context.router import _DOCKER_ERRORS as CONTEXT_DOCKER_ERRORS


class DomainError(Exception):
    status_code = 418
    detail = "domain operation failed"


class StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _raise(error: Exception) -> None:
    raise error


def _policy(
    api_error: PassThroughApiError | ConflictApiError | None = None,
    container_error_detail: str | None = None,
) -> DockerErrorPolicy:
    return DockerErrorPolicy(
        domain_errors=(DomainError,),
        api_error=api_error or PassThroughApiError("Docker rejected the operation"),
        container_error_detail=container_error_detail,
    )


def _container_error() -> ContainerError:
    return ContainerError(None, 1, "command", "image", "container output")


def test_domain_error_uses_its_status_and_detail() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(DomainError()), _policy())

    assert raised.value.status_code == 418
    assert raised.value.detail == "domain operation failed"


def test_not_found_maps_to_docker_resource_not_found() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(NotFound("missing")), _policy())

    assert raised.value.status_code == 404
    assert raised.value.detail == "Docker resource not found"


def test_container_error_uses_configured_detail() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(
            lambda: _raise(_container_error()),
            _policy(container_error_detail="Helper container failed"),
        )

    assert raised.value.status_code == 502
    assert str(raised.value.detail).startswith("Helper container failed")


def test_container_error_without_detail_uses_the_pinned_daemon_unavailable_path() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(_container_error()), _policy())

    assert raised.value.status_code == 503
    assert raised.value.detail == "Docker daemon is unavailable"


@pytest.mark.parametrize(
    ("policy", "expected_prefix"),
    [
        (DELEGATION_DOCKER_ERRORS, "Delegation helper container failed"),
        (CONTEXT_DOCKER_ERRORS, "Context helper container failed"),
    ],
)
def test_folded_policy_container_errors_use_the_configured_prefix(
    policy: DockerErrorPolicy,
    expected_prefix: str,
) -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(_container_error()), policy)

    assert raised.value.status_code == 502
    assert str(raised.value.detail).startswith(expected_prefix)


def test_pass_through_api_error_uses_docker_status() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(
            lambda: _raise(APIError("missing", response=StubResponse(404))),
            _policy(),
        )

    assert raised.value.status_code == 404
    assert raised.value.detail == "Docker rejected the operation"


def test_pass_through_api_error_without_response_uses_bad_gateway() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(APIError("failed")), _policy())

    assert raised.value.status_code == 502
    assert raised.value.detail == "Docker rejected the operation"


@pytest.mark.parametrize(
    ("policy", "expected_detail"),
    [
        (DELEGATION_DOCKER_ERRORS, "Docker rejected the delegation operation"),
        (CONTEXT_DOCKER_ERRORS, "Docker rejected the context operation"),
    ],
)
def test_folded_policy_api_errors_pass_through_the_configured_detail(
    policy: DockerErrorPolicy,
    expected_detail: str,
) -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(
            lambda: _raise(APIError("failed", response=StubResponse(418))),
            policy,
        )

    assert raised.value.status_code == 418
    assert raised.value.detail == expected_detail


def test_conflict_api_error_uses_conflict_detail_for_docker_conflict() -> None:
    policy = _policy(
        api_error=ConflictApiError("Docker rejected the action because it conflicts")
    )

    with pytest.raises(HTTPException) as raised:
        docker_response(
            lambda: _raise(APIError("conflict", response=StubResponse(409))),
            policy,
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "Docker rejected the action because it conflicts"


def test_conflict_api_error_collapses_other_docker_statuses() -> None:
    policy = _policy(api_error=ConflictApiError("conflict"))

    with pytest.raises(HTTPException) as raised:
        docker_response(
            lambda: _raise(APIError("failed", response=StubResponse(500))),
            policy,
        )

    assert raised.value.status_code == 502
    assert raised.value.detail == "Docker rejected the request"


def test_conflict_api_error_without_response_uses_bad_gateway() -> None:
    policy = _policy(api_error=ConflictApiError("conflict"))

    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(APIError("failed")), policy)

    assert raised.value.status_code == 502
    assert raised.value.detail == "Docker rejected the request"


def test_docker_exception_uses_daemon_unavailable() -> None:
    with pytest.raises(HTTPException) as raised:
        docker_response(lambda: _raise(DockerException("failed")), _policy())

    assert raised.value.status_code == 503
    assert raised.value.detail == "Docker daemon is unavailable"


def test_success_response_passes_through() -> None:
    result = object()

    assert docker_response(lambda: result, _policy()) is result
