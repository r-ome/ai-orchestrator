import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from app.controller.store import ControllerStore
from app.previews._shared import _now


logger = logging.getLogger("uvicorn.error")
# Accepts an optional duration_ms kwarg so a step can report zero-duration reuse.
ProgressReporter = Callable[..., None]


def _record_preview_progress(
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    proposal_id: str,
    preview_id: str,
    status: str,
    step: str,
    message: str,
    level: str = "info",
    duration_ms: int | None = None,
    started_at: str | None = None,
) -> None:
    limited_message = message[-16_384:]
    log_method = logger.error if level == "error" else logger.info
    log_method(
        "Preview %s proposal %s [%s] %s",
        preview_id,
        proposal_id,
        step,
        limited_message,
    )
    payload: dict[str, Any] = {
        "preview_id": preview_id,
        "status": status,
        "level": level,
        "step": step,
        "message": limited_message,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if started_at is not None:
        payload["started_at"] = started_at
    controller_store.event(
        sandbox_id=sandbox_id,
        run_id=proposal_id,
        kind="preview.progress",
        payload=payload,
    )


def _ignore_progress(
    step: str,
    message: str,
    duration_ms: int | None = None,
    started_at: str | None = None,
) -> None:
    del step, message, duration_ms, started_at


@contextmanager
def _timed_step(
    report: ProgressReporter,
    step: str,
    message: str,
) -> Iterator[Callable[[str], None]]:
    """Times one preview-preparation step.

    Emits a start event carrying `started_at`, then a completion event
    carrying `duration_ms`. Yields a callable the caller can use to give the
    completion event a result-specific message; called with an empty string,
    the completion event reuses `message`. Emits nothing on failure — the
    caller's own error handling already records a `failed` step.
    """
    started_at = _now()
    started = time.monotonic()
    report(step, message, started_at=started_at)
    completion_message = message

    def finish(text: str) -> None:
        nonlocal completion_message
        if text:
            completion_message = text

    yield finish
    duration_ms = int((time.monotonic() - started) * 1000)
    report(step, completion_message, duration_ms=duration_ms, started_at=started_at)
