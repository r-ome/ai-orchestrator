"""One writable coding turn, run to completion without a human at the terminal.

The planning runner next door mounts the sandbox read-only and allows only
Read, Glob and Grep. This one has to write, so it mounts `rw` and allows the
editing tools. The container hardening is otherwise the same, and the two
blocks are deliberately alike: if one is tightened, tighten the other.

What this does not do is decide whether the turn worked. A provider can finish
cleanly with every tool call failing and still answer, so the result carries
the tool outcomes and the caller reads the branch to find out what really
happened.
"""

import json
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from docker.errors import DockerException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from app.agents.config import get_agent_settings
from app.agents.models import AgentProvider
from app.agents.service import credential_volume
from app.planning.runner import extract_payload
from app.previews.service import LABEL_CONTROLLER_MANAGED, LABEL_KIND
from app.tasks.config import CodingTurnSettings

CODING_WORKSPACE = "/workspace"
CODING_CREDENTIALS = "/auth"
PROMPT_VARIABLE = "CODING_PROMPT"
LABEL_TASK_ID = "orchestrator.task.id"

WRITABLE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "MultiEdit", "Bash")


#: Why a turn ended. A turn that ran to completion is not necessarily one
#: that worked, which is what TOOL_FAILURE names.
SUCCEEDED = "succeeded"
TOOL_FAILURE = "tool_failure"
PROVIDER_FAILURE = "provider_failure"
TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class ToolCall:
    name: str
    failed: bool


@dataclass(frozen=True)
class TurnUsage:
    """Whatever the provider reported. A field it omits stays None rather than
    defaulting to zero, so a missing metric is never read as a measured one."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class CodingTurnResult:
    provider: AgentProvider
    #: The model the provider reports it used, which is the one worth keeping.
    model: str
    status: str
    text: str = ""
    payload: dict[str, Any] | None = None
    usage: TurnUsage = field(default_factory=TurnUsage)
    tool_calls: tuple[ToolCall, ...] = ()
    duration_ms: int | None = None
    exit_code: int | None = None
    error: str | None = None
    logs: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCEEDED


class CodingTurnError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def run_coding_turn(
    docker_client: Any,
    settings: CodingTurnSettings,
    *,
    task_id: str,
    volume_name: str,
    provider: AgentProvider,
    prompt: str,
    model: str | None = None,
) -> CodingTurnResult:
    """Run one turn with the sandbox volume writable.

    The caller checks the task branch out first; this runs on whatever branch
    the working tree is already on.
    """
    if provider is not AgentProvider.CLAUDE:
        raise CodingTurnError(
            501,
            f"Headless coding turns are not implemented for '{provider.value}'. "
            "Codex runs its own sandbox, which cannot start under cap_drop ALL "
            "and no-new-privileges; see docs/adr/0005-headless-coding-turns.md.",
        )

    resolved_model = model or settings.claude_model
    provider_config = get_agent_settings().provider(provider)
    credential = credential_volume(docker_client, provider, settings.credential_profile)
    container = None
    try:
        container = docker_client.containers.create(
            image=provider_config.image,
            command=_command(resolved_model),
            auto_remove=False,
            init=True,
            # The container is the boundary, not the provider's own sandbox:
            # read-only root, no capabilities, no new privileges, and only the
            # sandbox volume writable. Keep in step with app/planning/runner.py.
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=settings.pids_limit,
            mem_limit=settings.memory,
            working_dir=CODING_WORKSPACE,
            environment={
                provider_config.credential_environment_variable: CODING_CREDENTIALS,
                "HOME": "/tmp/home",
                "TERM": "dumb",
                PROMPT_VARIABLE: prompt,
            },
            labels={
                LABEL_CONTROLLER_MANAGED: "true",
                LABEL_KIND: "coding-turn",
                LABEL_TASK_ID: task_id,
            },
            volumes={
                volume_name: {"bind": CODING_WORKSPACE, "mode": "rw"},
                credential.name: {"bind": CODING_CREDENTIALS, "mode": "rw"},
            },
            tmpfs={"/tmp": "rw,nosuid,size=512m"},
        )
        container.start()
        timed_out = False
        exit_code: int | None = None
        try:
            status = container.wait(timeout=settings.timeout_seconds)
            exit_code = _exit_code(status)
        except (ReadTimeout, RequestsConnectionError):
            _kill(container)
            timed_out = True
        stdout = _logs(container, settings.max_log_bytes, stderr=False)
        stderr = _logs(container, settings.max_log_bytes, stderr=True)
    finally:
        _remove(container)

    return _result(
        provider=provider,
        model=resolved_model,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
    )


def _command(model: str) -> list[str]:
    """Write-capable flags for the Claude CLI.

    `stream-json` rather than `json`: the event stream is the only place
    per-tool-call outcomes appear, and those are what tell a real answer from
    one produced with every tool call failing.

    `acceptEdits` auto-approves file edits so an unattended turn does not stall
    on a prompt nobody can answer.
    """
    return [
        "sh",
        "-c",
        'exec claude -p "$CODING_PROMPT"'
        " --output-format stream-json --verbose"
        f" --model {shlex.quote(model)}"
        " --permission-mode acceptEdits"
        f' --allowedTools "{",".join(WRITABLE_TOOLS)}"'
        " < /dev/null",
    ]


def _result(
    *,
    provider: AgentProvider,
    model: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
) -> CodingTurnResult:
    events = _json_lines(stdout)
    tool_calls = _tool_calls(events)
    envelope = next(
        (event for event in reversed(events) if event.get("type") == "result"),
        None,
    )
    status, error = _status(
        envelope=envelope,
        tool_calls=tool_calls,
        exit_code=exit_code,
        timed_out=timed_out,
        stderr=stderr,
    )
    text = str(envelope.get("result") or "") if envelope else ""
    payload = None
    if status == SUCCEEDED and text:
        try:
            payload = extract_payload(text, provider=provider)
        except Exception:  # noqa: BLE001 - a missing result is not a failed turn
            payload = None
    return CodingTurnResult(
        provider=provider,
        model=_reported_model(envelope) or model,
        status=status,
        text=text,
        payload=payload,
        usage=_usage(envelope),
        tool_calls=tuple(tool_calls),
        duration_ms=_integer((envelope or {}).get("duration_ms")),
        exit_code=exit_code,
        error=error,
        logs=stderr[-4000:],
    )


def _status(
    *,
    envelope: Mapping[str, Any] | None,
    tool_calls: list[ToolCall],
    exit_code: int | None,
    timed_out: bool,
    stderr: str,
) -> tuple[str, str | None]:
    if timed_out:
        return TIMED_OUT, "Turn exceeded its timeout and was killed"
    if exit_code != 0:
        detail = stderr.strip()[-500:] or "no stderr"
        return PROVIDER_FAILURE, f"Provider exited with code {exit_code}: {detail}"
    if envelope is None:
        return PROVIDER_FAILURE, "Provider produced no result event"
    if envelope.get("is_error"):
        return PROVIDER_FAILURE, str(
            envelope.get("api_error_status") or envelope.get("subtype") or "error"
        )
    # A clean exit with every tool call failing means the answer rests on
    # nothing. Measured on Codex, whose sandbox could not start inside ours:
    # every command failed, and the model answered anyway.
    if tool_calls and all(call.failed for call in tool_calls):
        return (
            TOOL_FAILURE,
            f"All {len(tool_calls)} tool calls failed; the answer is unsupported",
        )
    return SUCCEEDED, None


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _tool_calls(events: list[dict[str, Any]]) -> list[ToolCall]:
    """Pair tool_use blocks with their tool_result by id.

    A tool_result carries `is_error` only when it failed, so an absent key
    means success. A tool_use with no matching result never completed and
    counts as failed.
    """
    names: dict[str, str] = {}
    failures: dict[str, bool] = {}
    for event in events:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name") or "unknown")
            elif block.get("type") == "tool_result":
                failures[str(block.get("tool_use_id"))] = bool(block.get("is_error"))
    return [
        ToolCall(name=name, failed=failures.get(call_id, True))
        for call_id, name in names.items()
    ]


def _usage(envelope: Mapping[str, Any] | None) -> TurnUsage:
    if envelope is None:
        return TurnUsage()
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    cost = envelope.get("total_cost_usd")
    return TurnUsage(
        input_tokens=_integer(usage.get("input_tokens")),
        output_tokens=_integer(usage.get("output_tokens")),
        cache_read_tokens=_integer(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_integer(usage.get("cache_creation_input_tokens")),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def _reported_model(envelope: Mapping[str, Any] | None) -> str | None:
    """The model the provider says it used, by output volume.

    A turn can touch more than one model; the one that produced the answer is
    the one worth recording.
    """
    if envelope is None:
        return None
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, Mapping) or not model_usage:
        return None
    return max(
        model_usage.items(),
        key=lambda item: (
            _integer(item[1].get("outputTokens")) or 0
            if isinstance(item[1], Mapping)
            else 0
        ),
    )[0]


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _kill(container: Any) -> None:
    try:
        container.kill()
    except DockerException:
        pass


def _logs(container: Any, max_bytes: int, *, stderr: bool) -> str:
    try:
        raw = container.logs(stdout=not stderr, stderr=stderr)
    except DockerException:
        return ""
    if isinstance(raw, bytes):
        return raw[-max_bytes:].decode("utf-8", errors="replace")
    return str(raw)[-max_bytes:]


def _remove(container: Any) -> None:
    """Removes the container without masking why the turn failed."""
    if container is None:
        return
    try:
        container.remove(force=True)
    except DockerException:
        pass


def _exit_code(status: Any) -> int:
    if isinstance(status, dict):
        return int(status.get("StatusCode", 1))
    return int(status)


def run_with_repair(
    run: Callable[[str], CodingTurnResult],
    *,
    prompt: str,
    validate: Callable[[Mapping[str, Any]], list[str]],
) -> tuple[CodingTurnResult, list[CodingTurnResult], list[str]]:
    """Run a turn, validate its payload, and allow exactly one repair.

    Bounded on purpose: one retry, then fail. Returns the accepted or final
    result, every attempt made, and any validation errors that survived.
    """
    attempts: list[CodingTurnResult] = []
    first = run(prompt)
    attempts.append(first)
    errors = _validation_errors(first, validate)
    if not errors:
        return first, attempts, []

    retry_prompt = prompt if not first.succeeded else _repair_prompt(prompt, errors)
    second = run(retry_prompt)
    attempts.append(second)
    return second, attempts, _validation_errors(second, validate)


def _validation_errors(
    result: CodingTurnResult,
    validate: Callable[[Mapping[str, Any]], list[str]],
) -> list[str]:
    if not result.succeeded:
        return [result.error or f"Turn ended as '{result.status}'"]
    if result.payload is None:
        return ["Turn output did not contain a JSON object"]
    return list(validate(result.payload))


def _repair_prompt(prompt: str, errors: list[str]) -> str:
    listed = "\n".join(f"- {error}" for error in errors)
    return (
        f"{prompt}\n\n"
        "Your previous response was rejected by validation:\n"
        f"{listed}\n\n"
        "Return a corrected response. Fix only these problems."
    )
