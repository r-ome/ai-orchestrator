import json
from typing import Any

from app.agents.models import AgentProvider
from app.tasks.runner import (
    PROVIDER_FAILURE,
    SUCCEEDED,
    TIMED_OUT,
    TOOL_FAILURE,
    WRITABLE_TOOLS,
    CodingTurnResult,
    _command,
    _environment,
    _result,
    run_with_repair,
)


def _event(**fields: Any) -> str:
    return json.dumps(fields)


def _tool_use(call_id: str, name: str) -> str:
    return _event(
        type="assistant",
        message={"content": [{"type": "tool_use", "id": call_id, "name": name}]},
    )


def _tool_result(call_id: str, *, is_error: bool | None) -> str:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id}
    if is_error is not None:
        block["is_error"] = is_error
    return _event(type="user", message={"content": [block]})


def _envelope(**overrides: Any) -> str:
    envelope: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "duration_ms": 1234,
        "total_cost_usd": 0.0216,
        "usage": {
            "input_tokens": 6,
            "output_tokens": 356,
            "cache_read_input_tokens": 41574,
            "cache_creation_input_tokens": 7902,
        },
        "modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 356}},
    }
    envelope.update(overrides)
    return _event(**envelope)


def _run(stdout: str, *, exit_code: int | None = 0, timed_out: bool = False):
    return _result(
        provider=AgentProvider.CLAUDE,
        model="claude-sonnet-5",
        stdout=stdout,
        stderr="",
        exit_code=exit_code,
        timed_out=timed_out,
    )


def _codex_item(
    event_type: str,
    item_id: str,
    item_type: str,
    **fields: Any,
) -> str:
    return _event(
        type=event_type,
        item={"id": item_id, "type": item_type, **fields},
    )


def _codex_completed() -> str:
    return _event(
        type="turn.completed",
        usage={
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        },
    )


def _run_codex(
    stdout: str,
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
):
    return _result(
        provider=AgentProvider.CODEX,
        model="gpt-5.6-terra",
        stdout=stdout,
        stderr="",
        exit_code=exit_code,
        timed_out=timed_out,
        measured_duration_ms=2345,
    )


def test_a_successful_turn_captures_cost_usage_and_the_reported_model() -> None:
    result = _run(
        "\n".join(
            [_tool_use("t1", "Edit"), _tool_result("t1", is_error=None), _envelope()]
        )
    )

    assert result.status == SUCCEEDED
    assert result.text == "done"
    assert result.duration_ms == 1234
    assert result.usage.cost_usd == 0.0216
    assert result.usage.input_tokens == 6
    assert result.usage.cache_read_tokens == 41574
    # The model the provider says it used, not the one requested.
    assert result.model == "claude-haiku-4-5-20251001"
    assert [call.failed for call in result.tool_calls] == [False]


def test_a_turn_with_every_tool_call_failing_is_not_a_success() -> None:
    """Measured on Codex: its sandbox could not start inside ours, every
    command failed, and the model answered anyway with exit code 0."""
    stdout = "\n".join(
        [
            _tool_use("t1", "Bash"),
            _tool_result("t1", is_error=True),
            _tool_use("t2", "Bash"),
            _tool_result("t2", is_error=True),
            _envelope(result="0"),
        ]
    )

    result = _run(stdout)

    assert result.status == TOOL_FAILURE
    assert result.payload is None
    assert result.error is not None
    assert "2 tool calls failed" in result.error


def test_a_partial_tool_failure_still_succeeds() -> None:
    stdout = "\n".join(
        [
            _tool_use("t1", "Edit"),
            _tool_result("t1", is_error=True),
            _tool_use("t2", "Read"),
            _tool_result("t2", is_error=None),
            _envelope(),
        ]
    )

    assert _run(stdout).status == SUCCEEDED


def test_a_tool_use_without_a_result_counts_as_failed() -> None:
    assert (
        _run("\n".join([_tool_use("t1", "Edit"), _envelope()])).status == TOOL_FAILURE
    )


def test_a_turn_with_no_tool_calls_succeeds() -> None:
    assert _run(_envelope()).status == SUCCEEDED


def test_timeout_and_non_zero_exit_are_distinct_failures() -> None:
    assert _run("", timed_out=True).status == TIMED_OUT
    assert _run("", exit_code=1).status == PROVIDER_FAILURE


def test_a_missing_result_event_is_a_provider_failure() -> None:
    result = _run(_tool_use("t1", "Edit"))

    assert result.status == PROVIDER_FAILURE
    assert result.error == "Provider produced no result event"


def test_a_provider_reported_error_is_a_provider_failure() -> None:
    assert _run(_envelope(is_error=True, subtype="error_during_execution")).status == (
        PROVIDER_FAILURE
    )


def test_non_json_lines_are_ignored() -> None:
    assert _run(f"not json\n\n  \n{_envelope()}").status == SUCCEEDED


def test_a_json_result_is_extracted_when_the_turn_reports_one() -> None:
    result = _run(_envelope(result='Here you go:\n{"changed": ["src/app.py"]}'))

    assert result.payload == {"changed": ["src/app.py"]}


def test_prose_without_json_leaves_the_payload_empty_rather_than_failing() -> None:
    """The commit is what matters and git verifies it separately. A missing
    self-report is a gap, not a failed turn."""
    result = _run(_envelope(result="I changed the file."))

    assert result.status == SUCCEEDED
    assert result.payload is None


def test_a_successful_codex_turn_captures_events_usage_and_payload() -> None:
    stdout = "\n".join(
        [
            _event(type="thread.started", thread_id="thread-1"),
            _event(type="turn.started"),
            _codex_item(
                "item.started",
                "item-1",
                "command_execution",
                command="git status --short",
                status="in_progress",
            ),
            _codex_item(
                "item.completed",
                "item-1",
                "command_execution",
                command="git status --short",
                status="completed",
                exit_code=0,
            ),
            _codex_item(
                "item.completed",
                "item-2",
                "agent_message",
                text='Done. {"changed": ["src/app.py"]}',
            ),
            _codex_completed(),
        ]
    )

    result = _run_codex(stdout)

    assert result.status == SUCCEEDED
    assert result.text == 'Done. {"changed": ["src/app.py"]}'
    assert result.payload == {"changed": ["src/app.py"]}
    assert result.model == "gpt-5.6-terra"
    assert result.duration_ms == 2345
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_tokens == 60
    assert result.usage.output_tokens == 20
    assert result.usage.cost_usd is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "Bash"
    assert result.tool_calls[0].failed is False


def test_codex_can_recover_from_one_failed_command() -> None:
    stdout = "\n".join(
        [
            _codex_item(
                "item.completed",
                "item-1",
                "command_execution",
                status="failed",
                exit_code=1,
            ),
            _codex_item(
                "item.completed",
                "item-2",
                "file_change",
                status="completed",
            ),
            _codex_item("item.completed", "item-3", "agent_message", text="done"),
            _codex_completed(),
        ]
    )

    result = _run_codex(stdout)

    assert result.status == SUCCEEDED
    assert [call.failed for call in result.tool_calls] == [True, False]


def test_codex_rejects_a_turn_when_every_tool_item_failed() -> None:
    stdout = "\n".join(
        [
            _codex_item(
                "item.started",
                "item-1",
                "command_execution",
                status="in_progress",
            ),
            _codex_item("item.completed", "item-2", "agent_message", text="done"),
            _codex_completed(),
        ]
    )

    result = _run_codex(stdout)

    assert result.status == TOOL_FAILURE
    assert result.error == "All 1 tool calls failed; the answer is unsupported"


def test_codex_reports_a_failed_turn_detail() -> None:
    result = _run_codex(
        _event(type="turn.failed", error={"message": "model unavailable"})
    )

    assert result.status == PROVIDER_FAILURE
    assert result.error == "model unavailable"


def test_the_command_is_writable_and_closes_stdin() -> None:
    command = _command(AgentProvider.CLAUDE, "claude-sonnet-5", "medium")

    assert command[0] == "sh"
    script = command[2]
    assert "--permission-mode acceptEdits" in script
    assert "Edit,Write" in script
    assert "Bash" in script
    # stream-json is what exposes per-tool outcomes; plain json does not.
    assert "--output-format stream-json --verbose" in script
    assert "< /dev/null" in script
    # The prompt travels by environment variable, never interpolated into the
    # shell, so a prompt containing quotes or backticks cannot break out.
    assert '"$CODING_PROMPT"' in script


def test_the_write_allowance_includes_the_editing_tools() -> None:
    assert {"Edit", "Write", "Bash"} <= set(WRITABLE_TOOLS)


def test_coding_turns_use_image_owned_browser_tooling() -> None:
    environment = _environment("CLAUDE_CONFIG_DIR", "do the work")

    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == "/ms-playwright"
    assert environment["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert environment["NODE_PATH"] == "/usr/local/lib/node_modules"
    assert environment["CI"] == "1"
    assert environment["CLAUDE_CONFIG_DIR"] == "/auth"
    assert environment["CODING_PROMPT"] == "do the work"


def test_the_codex_command_uses_the_outer_container_as_its_sandbox() -> None:
    command = _command(AgentProvider.CODEX, "gpt-5.6-terra", "high")

    assert command[0] == "sh"
    script = command[2]
    assert 'codex exec "$CODING_PROMPT"' in script
    assert "--json" in script
    assert "--sandbox danger-full-access" in script
    assert "--ephemeral" in script
    assert "--skip-git-repo-check" in script
    assert "-C /workspace" in script
    assert "-m gpt-5.6-terra" in script
    assert "-c model_reasoning_effort=high" in script
    assert "< /dev/null" in script


class _Recorder:
    def __init__(self, results: list[CodingTurnResult]) -> None:
        self.results = results
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> CodingTurnResult:
        self.prompts.append(prompt)
        return self.results[len(self.prompts) - 1]


def _succeeded(payload: dict[str, Any]) -> CodingTurnResult:
    return CodingTurnResult(
        provider=AgentProvider.CLAUDE, model="m", status=SUCCEEDED, payload=payload
    )


def _failed() -> CodingTurnResult:
    return CodingTurnResult(
        provider=AgentProvider.CLAUDE, model="m", status=PROVIDER_FAILURE, error="boom"
    )


def _requires_changed(payload: Any) -> list[str]:
    return [] if payload.get("changed") else ["'changed' is required"]


def test_repair_returns_the_first_valid_turn_without_retrying() -> None:
    recorder = _Recorder([_succeeded({"changed": ["x"]})])

    result, attempts, errors = run_with_repair(
        recorder, prompt="go", validate=_requires_changed
    )

    assert errors == []
    assert len(attempts) == 1
    assert recorder.prompts == ["go"]
    assert result.succeeded


def test_repair_feeds_validation_errors_back_exactly_once() -> None:
    recorder = _Recorder([_succeeded({}), _succeeded({"changed": ["x"]})])

    _result, attempts, errors = run_with_repair(
        recorder, prompt="go", validate=_requires_changed
    )

    assert errors == []
    assert len(attempts) == 2
    assert "'changed' is required" in recorder.prompts[1]


def test_repair_stops_after_the_second_attempt() -> None:
    recorder = _Recorder([_succeeded({}), _succeeded({})])

    _result, attempts, errors = run_with_repair(
        recorder, prompt="go", validate=_requires_changed
    )

    assert errors == ["'changed' is required"]
    assert len(attempts) == 2


def test_a_provider_failure_retries_with_the_original_prompt() -> None:
    recorder = _Recorder([_failed(), _succeeded({"changed": ["x"]})])

    _result, _attempts, errors = run_with_repair(
        recorder, prompt="go", validate=_requires_changed
    )

    assert errors == []
    # A provider failure is transient, so the prompt is not rewritten.
    assert recorder.prompts == ["go", "go"]
