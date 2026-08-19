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

from app.agents.config import get_agent_settings
from app.agents.models import AgentProvider
from app.agents.service import credential_volume
from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened
from app.controller.store import ControllerStore
from app.planning.runner import extract_payload
from app.platform.labels import LABEL_CONTROLLER_MANAGED, LABEL_KIND
from app.sandboxes.database import SandboxDatabaseError, sandbox_database_runtime
from app.tasks.config import CodingTurnSettings

CODING_WORKSPACE = "/workspace"
CODING_CREDENTIALS = "/auth"
PROMPT_VARIABLE = "CODING_PROMPT"
LABEL_TASK_ID = "orchestrator.task.id"
PLAYWRIGHT_BROWSERS_PATH = "/ms-playwright"
GLOBAL_NODE_MODULES = "/usr/local/lib/node_modules"

WRITABLE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "MultiEdit", "Bash")

# Codex reports tool work as item events. Agent messages and reasoning are not
# tool calls, so they do not belong here. Keep these names aligned with the
# documented `codex exec --json` item types.
CODEX_TOOL_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "mcp_tool_call", "web_search"}
)


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
    #: The reported model when available, otherwise the requested model.
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
    controller_store: ControllerStore | None = None,
    sandbox_id: str = "",
) -> CodingTurnResult:
    """Run one turn with the sandbox volume writable.

    The caller checks the task branch out first; this runs on whatever branch
    the working tree is already on.
    """
    resolved_model = model or settings.model(provider.value)
    provider_config = get_agent_settings().provider(provider)
    credential = credential_volume(docker_client, provider, settings.credential_profile)
    try:
        database_runtime = (
            sandbox_database_runtime(
                docker_client,
                controller_store,
                sandbox_id,
            )
            if controller_store is not None and sandbox_id
            else None
        )
    except SandboxDatabaseError as error:
        raise CodingTurnError(error.status_code, error.detail) from error
    environment = _environment(
        provider_config.credential_environment_variable,
        prompt,
    )
    volumes = {
        volume_name: {"bind": CODING_WORKSPACE, "mode": "rw"},
        credential.name: {"bind": CODING_CREDENTIALS, "mode": "rw"},
    }
    if database_runtime is not None:
        environment.update(database_runtime.environment)
        volumes.update(database_runtime.volumes)
    result = run_hardened(
        docker_client,
        HardenedRunSpec(
            image=provider_config.image,
            command=_command(
                provider,
                resolved_model,
                settings.codex_reasoning_effort,
            ),
            mem_limit=settings.memory,
            working_dir=CODING_WORKSPACE,
            environment=environment,
            labels={
                LABEL_CONTROLLER_MANAGED: "true",
                LABEL_KIND: "coding-turn",
                LABEL_TASK_ID: task_id,
            },
            volumes=volumes,
            timeout_seconds=settings.timeout_seconds,
            max_log_bytes=settings.max_log_bytes,
            tmpfs_size="512m",
            egress=Egress.PROVIDER,
            network=(
                database_runtime.network_name
                if database_runtime is not None and database_runtime.engine != "sqlite"
                else None
            ),
            capture=Capture.SEPARATE,
        ),
    )

    return _result(
        provider=provider,
        model=resolved_model,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code if result.exit_code is not None else 1,
        timed_out=result.timed_out,
        measured_duration_ms=result.duration_ms,
    )


def _environment(credential_variable: str, prompt: str) -> dict[str, str]:
    """Build the stable coding environment, including image-owned browser tools."""
    return {
        credential_variable: CODING_CREDENTIALS,
        "HOME": "/tmp/home",
        "TERM": "dumb",
        "CI": "1",
        "NODE_PATH": GLOBAL_NODE_MODULES,
        "PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_PATH,
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        PROMPT_VARIABLE: prompt,
    }


def _command(
    provider: AgentProvider,
    model: str,
    codex_reasoning_effort: str,
) -> list[str]:
    """Build a non-interactive, write-capable provider command.

    The hardened Docker container is the security boundary for both providers.
    Codex therefore runs without its nested sandbox, which cannot start under
    cap_drop=ALL and no-new-privileges. The task volume is the only writable
    project path in that outer container.

    Both providers emit event streams. The controller uses those events to
    distinguish a real result from an answer produced after every tool failed.
    """
    if provider is AgentProvider.CLAUDE:
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
    return [
        "sh",
        "-c",
        'exec codex exec "$CODING_PROMPT"'
        " --json"
        " --sandbox danger-full-access"
        " --ephemeral"
        " --skip-git-repo-check"
        f" -C {CODING_WORKSPACE}"
        f" -m {shlex.quote(model)}"
        f" -c model_reasoning_effort={shlex.quote(codex_reasoning_effort)}"
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
    measured_duration_ms: int | None = None,
) -> CodingTurnResult:
    events = _json_lines(stdout)
    if provider is AgentProvider.CODEX:
        return _codex_result(
            model=model,
            events=events,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            measured_duration_ms=measured_duration_ms,
        )

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
    reported_duration_ms = _integer((envelope or {}).get("duration_ms"))
    return CodingTurnResult(
        provider=provider,
        model=_reported_model(envelope) or model,
        status=status,
        text=text,
        payload=payload,
        usage=_usage(envelope),
        tool_calls=tuple(tool_calls),
        duration_ms=(
            reported_duration_ms
            if reported_duration_ms is not None
            else measured_duration_ms
        ),
        exit_code=exit_code,
        error=error,
        logs=stderr[-4000:],
    )


def _codex_result(
    *,
    model: str,
    events: list[dict[str, Any]],
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    measured_duration_ms: int | None,
) -> CodingTurnResult:
    tool_calls = _codex_tool_calls(events)
    completed = next(
        (event for event in reversed(events) if event.get("type") == "turn.completed"),
        None,
    )
    failed = next(
        (event for event in reversed(events) if event.get("type") == "turn.failed"),
        None,
    )
    status, error = _codex_status(
        completed=completed,
        failed=failed,
        events=events,
        tool_calls=tool_calls,
        exit_code=exit_code,
        timed_out=timed_out,
        stderr=stderr,
    )
    text = _codex_text(events)
    payload = None
    if status == SUCCEEDED and text:
        try:
            payload = extract_payload(text, provider=AgentProvider.CODEX)
        except Exception:  # noqa: BLE001 - the branch remains authoritative
            payload = None
    return CodingTurnResult(
        provider=AgentProvider.CODEX,
        model=model,
        status=status,
        text=text,
        payload=payload,
        usage=_codex_usage(completed),
        tool_calls=tuple(tool_calls),
        duration_ms=measured_duration_ms,
        exit_code=exit_code,
        error=error,
        logs=stderr[-4000:],
    )


def _codex_status(
    *,
    completed: Mapping[str, Any] | None,
    failed: Mapping[str, Any] | None,
    events: list[dict[str, Any]],
    tool_calls: list[ToolCall],
    exit_code: int | None,
    timed_out: bool,
    stderr: str,
) -> tuple[str, str | None]:
    if timed_out:
        return TIMED_OUT, "Turn exceeded its timeout and was killed"
    if exit_code != 0:
        detail = _codex_error(events) or stderr.strip()[-500:] or "no stderr"
        return PROVIDER_FAILURE, f"Provider exited with code {exit_code}: {detail}"
    if failed is not None:
        return PROVIDER_FAILURE, _error_detail(failed) or "Codex reported a failed turn"
    if completed is None:
        return (
            PROVIDER_FAILURE,
            _codex_error(events) or "Provider produced no completed turn",
        )
    if tool_calls and all(call.failed for call in tool_calls):
        return (
            TOOL_FAILURE,
            f"All {len(tool_calls)} tool calls failed; the answer is unsupported",
        )
    return SUCCEEDED, None


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
    # nothing. The process can still produce a final message in that state.
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


def _codex_tool_calls(events: list[dict[str, Any]]) -> list[ToolCall]:
    """Pair Codex item starts and completions by item id.

    A started item without a completion failed. A completed command also
    failed when it reports a non-zero exit code, even if its status field is
    absent. Codex can recover after one failed command, so the caller rejects
    the turn only when every tool item failed.
    """
    names: dict[str, str] = {}
    failures: dict[str, bool] = {}
    for event in events:
        if event.get("type") not in {"item.started", "item.completed"}:
            continue
        item = event.get("item")
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        if item_type not in CODEX_TOOL_ITEM_TYPES:
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        names[item_id] = _codex_tool_name(item_type)
        if event.get("type") == "item.completed":
            status = str(item.get("status") or "completed").lower()
            exit_code = _integer(item.get("exit_code"))
            failures[item_id] = status in {
                "failed",
                "error",
                "cancelled",
                "canceled",
            } or (exit_code is not None and exit_code != 0)
    return [
        ToolCall(name=name, failed=failures.get(item_id, True))
        for item_id, name in names.items()
    ]


def _codex_tool_name(item_type: str) -> str:
    return {
        "command_execution": "Bash",
        "file_change": "ApplyPatch",
        "mcp_tool_call": "MCP",
        "web_search": "WebSearch",
    }.get(item_type, item_type)


def _codex_text(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            return str(item.get("text") or "")
    return ""


def _codex_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "error":
            return _error_detail(event)
    return None


def _error_detail(event: Mapping[str, Any]) -> str | None:
    error = event.get("error")
    if isinstance(error, Mapping):
        for key in ("message", "detail", "code"):
            if error.get(key):
                return str(error[key])
    if error:
        return str(error)
    for key in ("message", "detail"):
        if event.get(key):
            return str(event[key])
    return None


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


def _codex_usage(completed: Mapping[str, Any] | None) -> TurnUsage:
    if completed is None:
        return TurnUsage()
    usage = completed.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    return TurnUsage(
        input_tokens=_integer(usage.get("input_tokens")),
        output_tokens=_integer(usage.get("output_tokens")),
        cache_read_tokens=_integer(usage.get("cached_input_tokens")),
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
    return (
        int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    )


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
