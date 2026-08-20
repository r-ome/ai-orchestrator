"""Parse one agent provider's stdout, keyed on AgentProvider.

This lives in agents because it parses provider stdout. Keeping it in
planning/runner.py put tasks -> planning into the app import cycle for one
symbol.
"""

import json
from typing import Any

from app.agents.models import AgentProvider


class AgentOutputError(Exception):
    def __init__(self, status_code: int, detail: str, raw_output: str = "") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.raw_output = raw_output


def extract_payload(raw: str, *, provider: AgentProvider) -> dict[str, Any]:
    text = raw
    if provider is AgentProvider.CLAUDE:
        try:
            envelope = json.loads(raw)
            result = envelope["result"]
            if not isinstance(result, str):
                raise TypeError("Claude result is not text")
            text = result
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    # The last object, not the first. Codex echoes the prompt into its
    # transcript, and the prompt carries the JSON schema as a worked example,
    # so the first object in the output is that example rather than the
    # reply. The reply is last: the command cats the last-message file after
    # the transcript. Claude's envelope leaves only the reply text here, so
    # taking the last object costs it nothing.
    span = _last_object_span(text)
    if span is None:
        if "{" in text:
            raise AgentOutputError(422, "Unterminated JSON object in model output", raw)
        raise AgentOutputError(422, "No JSON object found in model output", raw)
    start, end = span
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise AgentOutputError(
            422, f"Invalid JSON payload: {error.msg}", raw
        ) from error
    if not isinstance(payload, dict):
        raise AgentOutputError(422, "JSON payload must be an object", raw)
    return payload


def _last_object_span(text: str) -> tuple[int, int] | None:
    """The span of the last complete top-level `{...}` in the text.

    Nested objects never win: each match jumps the scan past the whole
    object it opened. A truncated object at the very end is skipped in
    favour of the last complete one before it.

    A `{` that never closes does not end the scan, it is only skipped. A
    codex transcript quotes the files and shell commands the turn read, and
    those carry lone braces — `<BaseHead title={SITE_TITLE} />` is one. Ending
    the scan there would return some fragment of the transcript instead of the
    reply that follows it.
    """
    span: tuple[int, int] | None = None
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            break
        end = _balanced_object_end(text, start)
        if end is None:
            position = start + 1
            continue
        span = (start, end)
        position = end + 1
    return span


def _balanced_object_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
    return None
