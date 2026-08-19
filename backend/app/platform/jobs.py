"""Background execution for turns that outlive an HTTP request.

A coding turn waits on `container.wait(timeout=CODING_TURN_TIMEOUT_SECONDS)`,
which defaults to 1800 seconds and runs twice when a provider fails. No browser
holds a connection open that long: the fetch dies and the caller loses the
response even though the turn itself finishes normally. So the request claims
the work, records it, and hands the turn to a thread here.

Threads rather than an async task queue because every runner in this codebase
is blocking `docker` client code, and the store is SQLite reached through its
own per-thread connections. There is no external broker to keep in step, and
the claimed database row is the job record: progress arrives as events on that
row, and `reconcile_controller_state` settles anything a restart interrupted.
"""

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import docker
from docker.client import DockerClient
from docker.errors import DockerException


logger = logging.getLogger(__name__)

_threads: set[threading.Thread] = set()
_threads_lock = threading.Lock()
# Process-wide rather than thread-local on purpose: FastAPI runs a sync
# endpoint on an anyio worker thread, so a flag set by a test would not be
# visible where `submit` is actually called.
_inline = False


def submit(work: Callable[[], None], *, name: str) -> None:
    """Runs `work` on a daemon thread and returns at once.

    Exceptions are logged rather than raised: the caller has already sent its
    response, so nothing is left to receive them. Every execute function this
    dispatches settles its own row to a failed status first, which is what the
    reader actually sees.
    """
    if _inline:
        _guard(work, name)()
        return
    thread = threading.Thread(target=_guard(work, name), name=name, daemon=True)
    with _threads_lock:
        _threads.add(thread)
    thread.start()


def submit_docker_job(
    work: Callable[[DockerClient], None],
    *,
    name: str,
    on_setup_error: Callable[[str], None],
) -> None:
    """Run `work` in the background against a client the job itself owns.

    The request's own client cannot be reused: `get_docker_client` is a
    generator dependency, so FastAPI calls `client.close()` the moment the
    response is sent — long before a background turn is done with it.

    `on_setup_error` settles the claimed row when no client can be built at
    all. Without it the row would sit in its in-flight status until the next
    restart, which is exactly the deadlock this work set out to remove.
    """

    def run() -> None:
        try:
            client = docker.from_env()
        except DockerException as error:
            logger.exception("Background job '%s' could not reach Docker", name)
            on_setup_error(str(error) or "Docker daemon is unavailable")
            return
        try:
            work(client)
        finally:
            client.close()

    submit(run, name=name)


def _guard(work: Callable[[], None], name: str) -> Callable[[], None]:
    def run() -> None:
        try:
            work()
        except Exception:
            logger.exception("Background job '%s' failed", name)
        finally:
            with _threads_lock:
                _threads.discard(threading.current_thread())

    return run


def wait_for_jobs(timeout: float = 60.0) -> bool:
    """Joins every in-flight job. Returns False if any outlived the timeout.

    Tests use this to make a 202 endpoint assertable. Nothing in the request
    path calls it — a job that has not finished is reported through its row.
    """
    with _threads_lock:
        pending = list(_threads)
    for thread in pending:
        thread.join(timeout)
        if thread.is_alive():
            return False
    return True


@contextmanager
def run_jobs_inline() -> Iterator[None]:
    """Runs submitted jobs on the calling thread for the duration of the block.

    Turns the claim-then-dispatch split back into one synchronous call, so a
    test can drive an endpoint end to end without joining threads or polling.
    """
    global _inline
    previous = _inline
    _inline = True
    try:
        yield
    finally:
        _inline = previous


__all__ = ["run_jobs_inline", "submit", "submit_docker_job", "wait_for_jobs"]
