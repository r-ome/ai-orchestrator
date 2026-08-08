import json
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from docker.errors import DockerException
from requests.exceptions import ReadTimeout

from app.agents.config import get_agent_settings
from app.agents.models import AgentProvider
from app.agents.service import credential_volume
from app.planning.config import PlanningSettings
from app.planning.models import PlanningRole
from app.previews.service import LABEL_CONTROLLER_MANAGED, LABEL_KIND

PLANNING_WORKSPACE = "/workspace"
PLANNING_CREDENTIALS = "/auth"
PROMPT_VARIABLE = "PLANNING_PROMPT"
LABEL_SESSION_ID = "orchestrator.planning.session-id"
LABEL_ROLE = "orchestrator.planning.role"


@dataclass(frozen=True)
class TurnRequest:
    role: PlanningRole
    provider: AgentProvider
    prompt: str
    project_volume: str
    session_id: str = ""


@dataclass(frozen=True)
class TurnResult:
    raw_output: str
    payload: dict[str, Any]
    #: The model this turn actually ran, recorded at run time. Reading it back
    #: from settings later would report today's configuration, not the one the
    #: turn used.
    model: str = ""


class PlanningTurnError(Exception):
    def __init__(self, status_code: int, detail: str, raw_output: str = "") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.raw_output = raw_output


def run_planning_turn(
    docker_client: Any,
    settings: PlanningSettings,
    request: TurnRequest,
) -> TurnResult:
    provider = get_agent_settings().provider(request.provider)
    command = _command(request.provider, settings)
    credential = credential_volume(
        docker_client,
        request.provider,
        settings.credential_profile,
    )
    labels = {
        LABEL_CONTROLLER_MANAGED: "true",
        LABEL_KIND: "planning",
        LABEL_SESSION_ID: request.session_id,
        LABEL_ROLE: request.role.value,
    }
    container = None
    raw_output = ""
    try:
        container = docker_client.containers.create(
            image=provider.image,
            command=command,
            auto_remove=False,
            init=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=512,
            mem_limit=settings.planning_memory,
            working_dir=PLANNING_WORKSPACE,
            environment={
                provider.credential_environment_variable: PLANNING_CREDENTIALS,
                "HOME": "/tmp/home",
                "TERM": "dumb",
                PROMPT_VARIABLE: request.prompt,
            },
            labels=labels,
            volumes={
                request.project_volume: {"bind": PLANNING_WORKSPACE, "mode": "ro"},
                credential.name: {"bind": PLANNING_CREDENTIALS, "mode": "rw"},
            },
            tmpfs={"/tmp": "rw,nosuid,size=256m"},
        )
        container.start()
        try:
            status = container.wait(timeout=settings.turn_timeout_seconds)
        except ReadTimeout as error:
            container.kill()
            raise PlanningTurnError(
                504,
                f"{request.role.value} turn timed out after "
                f"{settings.turn_timeout_seconds} seconds",
            ) from error
        raw_output = _text(container.logs())
        exit_code = _exit_code(status)
        if exit_code != 0:
            tail = raw_output[-2000:]
            raise PlanningTurnError(
                502,
                f"{request.role.value} turn exited with status {exit_code}: {tail}",
                raw_output=raw_output,
            )
        return TurnResult(
            raw_output=raw_output,
            payload=extract_payload(raw_output, provider=request.provider),
            model=turn_model(request.provider, settings),
        )
    finally:
        _remove_container(container)


def _remove_container(container: Any) -> None:
    """Removes the turn's container without masking why the turn failed.

    This runs in a finally block, so an exception raised here would replace the
    timeout or non-zero exit that actually ended the turn. Mirrors
    _remove_created_container in app/agents/service.py.
    """
    if container is None:
        return
    try:
        container.remove(force=True)
    except DockerException:
        pass


def run_turn_with_repair(
    docker_client: Any,
    settings: PlanningSettings,
    request: TurnRequest,
    validate: Callable[[dict[str, Any]], None],
) -> TurnResult:
    current_request = request
    for attempt in range(2):
        try:
            result = run_planning_turn(docker_client, settings, current_request)
            try:
                validate(result.payload)
            except ValueError as error:
                raise PlanningTurnError(422, str(error), result.raw_output) from error
            return result
        except PlanningTurnError as error:
            if error.status_code != 422 or attempt == 1:
                raise
            current_request = TurnRequest(
                role=request.role,
                provider=request.provider,
                project_volume=request.project_volume,
                session_id=request.session_id,
                prompt=_repair_prompt(request.prompt, error.detail, error.raw_output),
            )
    raise AssertionError("repair loop must return or raise")


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
    start = text.find("{")
    if start < 0:
        raise PlanningTurnError(422, "No JSON object found in model output", raw)
    end = _balanced_object_end(text, start)
    if end is None:
        raise PlanningTurnError(422, "Unterminated JSON object in model output", raw)
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise PlanningTurnError(422, f"Invalid JSON payload: {error.msg}", raw) from error
    if not isinstance(payload, dict):
        raise PlanningTurnError(422, "JSON payload must be an object", raw)
    return payload


def turn_model(provider: AgentProvider, settings: PlanningSettings) -> str:
    """The model name passed to the provider's CLI for one turn.

    Codex carries its reasoning effort too, because the same model at a
    different effort is a different run and costs differently.
    """
    if provider is AgentProvider.CLAUDE:
        return settings.claude_model
    return f"{settings.codex_model} ({settings.codex_reasoning_effort} effort)"


def _command(provider: AgentProvider, settings: PlanningSettings) -> list[str]:
    if provider is AgentProvider.CLAUDE:
        return [
            "sh",
            "-c",
            'exec claude -p "$PLANNING_PROMPT"'
            " --output-format json"
            f" --model {shlex.quote(settings.claude_model)}"
            " --permission-mode plan"
            ' --allowedTools "Read,Glob,Grep"'
            " < /dev/null",
        ]
    return [
        "sh",
        "-c",
        'codex exec "$PLANNING_PROMPT"'
        " --sandbox read-only"
        " --ephemeral"
        " --skip-git-repo-check"
        " -C /workspace"
        f" -m {shlex.quote(settings.codex_model)}"
        f" -c model_reasoning_effort={shlex.quote(settings.codex_reasoning_effort)}"
        " --output-last-message /tmp/planning-output.json"
        " < /dev/null"
        " && cat /tmp/planning-output.json",
    ]


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


def _repair_prompt(original: str, error: str, raw_output: str) -> str:
    return "\n\n".join(
        [
            original,
            f"Your previous reply could not be used. Error: {error}",
            "Previous reply:\n" + raw_output[:4000],
            "Reply again with one JSON object and nothing else.",
        ]
    )


def _exit_code(status: Any) -> int:
    if isinstance(status, dict):
        return int(status.get("StatusCode", 1))
    return int(status)


def _text(value: bytes | str) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value
