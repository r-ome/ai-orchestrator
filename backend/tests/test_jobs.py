import pytest
from docker.errors import DockerException

from app.platform import jobs


def test_a_job_runs_off_the_calling_thread() -> None:
    seen: list[str] = []

    jobs.submit(lambda: seen.append("ran"), name="test")

    assert jobs.wait_for_jobs(5)
    assert seen == ["ran"]


def test_a_failing_job_does_not_escape_to_the_caller() -> None:
    """The response is already sent, so nothing is left to receive the error."""

    def boom() -> None:
        raise RuntimeError("boom")

    jobs.submit(boom, name="test")

    assert jobs.wait_for_jobs(5)


def test_run_jobs_inline_makes_a_claim_synchronous() -> None:
    seen: list[str] = []

    with jobs.run_jobs_inline():
        jobs.submit(lambda: seen.append("ran"), name="test")
        # Already done: nothing was queued to a thread.
        assert seen == ["ran"]


def test_a_docker_job_gets_its_own_client_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request's client is closed when the response is sent.

    `get_docker_client` is a generator dependency, so reusing it in the
    background would hand the turn an already-closed client.
    """
    closed: list[bool] = []

    class Client:
        def close(self) -> None:
            closed.append(True)

    built = Client()
    monkeypatch.setattr(jobs.docker, "from_env", lambda: built)
    received: list[object] = []

    with jobs.run_jobs_inline():
        jobs.submit_docker_job(
            received.append,
            name="test",
            on_setup_error=lambda detail: received.append(detail),
        )

    assert received == [built]
    assert closed == [True]


def test_a_docker_job_that_cannot_reach_docker_settles_its_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the claimed row sits in flight until the next restart."""

    def unavailable() -> None:
        raise DockerException("no daemon")

    monkeypatch.setattr(jobs.docker, "from_env", unavailable)
    errors: list[str] = []
    ran: list[object] = []

    with jobs.run_jobs_inline():
        jobs.submit_docker_job(
            ran.append,
            name="test",
            on_setup_error=errors.append,
        )

    assert ran == []
    assert errors == ["no daemon"]
