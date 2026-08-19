import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import DockerException
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from app.agents.models import AgentProvider
from app.planning.config import PlanningSettings
from app.planning.models import PlanningRole
from app.planning.runner import (
    PROMPT_VARIABLE,
    PlanningTurnError,
    TurnRequest,
    extract_payload,
    run_planning_turn,
    run_validated_turn,
)


class _StubContainer:
    def __init__(
        self,
        *,
        output: bytes = b'{"message": "ok"}',
        status: int = 0,
        wait_error: Exception | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self.output = output
        self.status = status
        self.wait_error = wait_error
        self.kill_error = kill_error
        self.started = False
        self.killed = False
        self.removed = False

    def start(self) -> None:
        self.started = True

    def wait(self, *, timeout: int) -> dict[str, int]:
        if self.wait_error is not None:
            raise self.wait_error
        return {"StatusCode": self.status}

    def logs(self, **_kwargs: Any) -> bytes:
        return self.output

    def kill(self) -> None:
        self.killed = True
        if self.kill_error is not None:
            raise self.kill_error

    def remove(self, *, force: bool) -> None:
        assert force is True
        self.removed = True


class _StubContainers:
    def __init__(self, queued: list[_StubContainer]) -> None:
        self.queued = queued
        self.create_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _StubContainer:
        self.create_calls.append(kwargs)
        return self.queued.pop(0)


class _StubDockerClient:
    def __init__(self, queued: list[_StubContainer]) -> None:
        self.containers = _StubContainers(queued)


@pytest.fixture
def settings() -> PlanningSettings:
    return PlanningSettings(
        clarifier_provider=AgentProvider.CLAUDE,
        planner_provider=AgentProvider.CLAUDE,
        reviewer_provider=AgentProvider.CODEX,
        credential_profile="default",
        max_review_turns=3,
        turn_timeout_seconds=600,
        planning_memory="2g",
        claude_model="opus",
        codex_model="gpt-5.6-terra",
        codex_reasoning_effort="high",
    )


@pytest.fixture(autouse=True)
def stub_agent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.planning import runner

    def provider(provider: AgentProvider) -> SimpleNamespace:
        if provider is AgentProvider.CLAUDE:
            return SimpleNamespace(
                image="claude-image",
                credential_environment_variable="CLAUDE_CONFIG_DIR",
            )
        return SimpleNamespace(
            image="codex-image",
            credential_environment_variable="CODEX_HOME",
        )

    monkeypatch.setattr(
        runner, "get_agent_settings", lambda: SimpleNamespace(provider=provider)
    )
    monkeypatch.setattr(
        runner, "credential_volume", lambda *_: SimpleNamespace(name="auth-volume")
    )


def _request(provider: AgentProvider = AgentProvider.CODEX) -> TurnRequest:
    return TurnRequest(
        role=PlanningRole.CLARIFIER,
        provider=provider,
        prompt="The prompt must only be in the environment.",
        project_volume="project-volume",
        session_id="session-1",
    )


def test_claude_command_has_read_only_planning_flags(
    settings: PlanningSettings,
) -> None:
    docker_client = _StubDockerClient(
        [_StubContainer(output=b'{"result": "{\\"message\\": \\"ok\\"}"}')]
    )

    run_planning_turn(docker_client, settings, _request(AgentProvider.CLAUDE))

    command = docker_client.containers.create_calls[0]["command"][2]
    assert "--permission-mode plan" in command
    assert "--allowedTools" in command
    assert "--output-format json" in command


def test_codex_command_ends_exec_with_closed_stdin(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient([_StubContainer()])

    run_planning_turn(docker_client, settings, _request())

    command = docker_client.containers.create_calls[0]["command"][2]
    # The container is the sandbox, not bubblewrap — see the note in _command.
    # bwrap cannot start under cap_drop=ALL plus no-new-privileges, and its
    # failure blocked codex from reading the workspace at all.
    assert "--sandbox danger-full-access" in command
    assert "< /dev/null && cat /tmp/planning-output.json" in command


def test_container_mounts_are_read_only_for_the_project(
    settings: PlanningSettings,
) -> None:
    docker_client = _StubDockerClient([_StubContainer()])

    run_planning_turn(docker_client, settings, _request())

    create_call = docker_client.containers.create_calls[0]
    assert create_call["volumes"] == {
        "project-volume": {"bind": "/workspace", "mode": "ro"},
        "auth-volume": {"bind": "/auth", "mode": "rw"},
    }
    assert "network_disabled" not in create_call


def test_prompt_is_in_the_environment_not_the_command(
    settings: PlanningSettings,
) -> None:
    docker_client = _StubDockerClient([_StubContainer()])
    request = _request()

    run_planning_turn(docker_client, settings, request)

    create_call = docker_client.containers.create_calls[0]
    assert create_call["environment"][PROMPT_VARIABLE] == request.prompt
    assert request.prompt not in create_call["command"][2]


def test_extract_payload_accepts_prose_around_json() -> None:
    payload = extract_payload(
        'Here is the reply. {"message": "ok", "questions": []} Thank you.',
        provider=AgentProvider.CODEX,
    )

    assert payload == {"message": "ok", "questions": []}


def test_extract_payload_ignores_braces_and_escapes_inside_strings() -> None:
    expected = {
        "plan_markdown": 'Use {value} and the literal \\"}\\" without ending JSON.',
        "scope": "Includes {braces}",
    }
    raw = "Model preamble: " + json.dumps(expected) + " trailing prose"

    assert extract_payload(raw, provider=AgentProvider.CODEX) == expected


def test_extract_payload_skips_the_schema_echoed_in_the_codex_transcript() -> None:
    """Codex prints the prompt back, and the prompt shows the schema by example.

    The schema is a complete JSON object, so a first-object scan reads the
    placeholder text as the clarifier's reply. The real reply is last.
    """
    reply = {"message": "I understand the action bar.", "questions": [], "ready": True}
    raw = "\n".join(
        [
            "JSON schema:",
            json.dumps(
                {"message": "one short paragraph to the human", "questions": []},
                indent=2,
            ),
            "Reply with one JSON object and nothing else.",
            "codex",
            json.dumps(reply),
            "tokens used",
            "3,674",
            json.dumps(reply),
        ]
    )

    assert extract_payload(raw, provider=AgentProvider.CODEX) == reply


def test_extract_payload_prefers_the_last_complete_object_over_a_truncated_tail() -> (
    None
):
    reply = {"message": "complete"}
    raw = json.dumps(reply) + '\n{"message": "cut off'

    assert extract_payload(raw, provider=AgentProvider.CODEX) == reply


def test_extract_payload_skips_a_lone_brace_in_the_transcript() -> None:
    """The transcript quotes files, and quoted files carry unbalanced braces.

    An Astro template attribute such as `title={SITE_TITLE}` reads as a
    complete object, and the `{` of `<style>{` that follows never closes.
    Both sit before the reply, so neither may end the scan.
    """
    reply = {"plan_markdown": "## Implementation plan", "scope": "the action bar"}
    raw = "\n".join(
        [
            "<BaseHead title={SITE_TITLE} />",
            "<style>{ unterminated in the quoted file",
            json.dumps(reply),
        ]
    )

    assert extract_payload(raw, provider=AgentProvider.CODEX) == reply


def test_malformed_output_runs_exactly_one_repair_turn(
    settings: PlanningSettings,
) -> None:
    first = _StubContainer(output=b"not JSON")
    second = _StubContainer(output=b'{"message": "repaired"}')
    docker_client = _StubDockerClient([first, second])

    request = _request()
    result = run_validated_turn(
        lambda prompt: run_planning_turn(
            docker_client,
            settings,
            replace(request, prompt=prompt),
        ),
        prompt=request.prompt,
        validate=lambda _: [],
    )

    assert result.accepted
    assert result.result is not None
    assert result.result.payload == {"message": "repaired"}
    assert len(docker_client.containers.create_calls) == 2
    repair_prompt = docker_client.containers.create_calls[1]["environment"][
        PROMPT_VARIABLE
    ]
    assert "Your previous reply was rejected:" in repair_prompt


def test_second_malformed_output_raises(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient(
        [_StubContainer(output=b"bad"), _StubContainer(output=b"still bad")]
    )

    request = _request()
    outcome = run_validated_turn(
        lambda prompt: run_planning_turn(
            docker_client,
            settings,
            replace(request, prompt=prompt),
        ),
        prompt=request.prompt,
        validate=lambda _: [],
    )

    assert not outcome.accepted
    assert outcome.result is None
    assert outcome.errors == ["No JSON object found in model output"]
    assert len(docker_client.containers.create_calls) == 2


def test_nonzero_exit_raises_with_log_tail(settings: PlanningSettings) -> None:
    output = ("discard-this-prefix" + "x" * 2100 + "last failure").encode()
    docker_client = _StubDockerClient([_StubContainer(output=output, status=17)])

    with pytest.raises(PlanningTurnError) as error:
        run_planning_turn(docker_client, settings, _request())

    assert error.value.status_code == 502
    assert "last failure" in error.value.detail
    assert "discard-this-prefix" not in error.value.detail


def test_timeout_kills_container_and_raises_504(settings: PlanningSettings) -> None:
    container = _StubContainer(wait_error=ReadTimeout("timed out"))
    docker_client = _StubDockerClient([container])

    with pytest.raises(PlanningTurnError) as error:
        run_planning_turn(docker_client, settings, _request())

    assert error.value.status_code == 504
    assert container.killed is True


def test_dropped_connection_while_waiting_is_reported_as_a_timeout(
    settings: PlanningSettings,
) -> None:
    """A lost connection tells the operator the same thing as a read timeout.

    Either way the turn never reported a result. Letting the requests error
    escape instead would surface as an unhandled 500.
    """
    container = _StubContainer(wait_error=RequestsConnectionError("connection lost"))
    docker_client = _StubDockerClient([container])

    with pytest.raises(PlanningTurnError) as error:
        run_planning_turn(docker_client, settings, _request())

    assert error.value.status_code == 504
    assert container.killed is True


def test_a_failed_kill_does_not_mask_the_timeout(settings: PlanningSettings) -> None:
    container = _StubContainer(
        wait_error=ReadTimeout("timed out"),
        kill_error=DockerException("daemon is gone"),
    )
    docker_client = _StubDockerClient([container])

    with pytest.raises(PlanningTurnError) as error:
        run_planning_turn(docker_client, settings, _request())

    assert error.value.status_code == 504
    assert container.removed is True


def test_log_is_capped_to_max_log_bytes(settings: PlanningSettings) -> None:
    """A turn that loops can print without bound, and the log is held in memory."""
    payload = b'{"message": "ok"}'
    capped = replace(settings, max_log_bytes=len(payload))
    container = _StubContainer(output=b"x" * 5000 + payload)
    docker_client = _StubDockerClient([container])

    result = run_planning_turn(docker_client, capped, _request())

    assert result.raw_output == payload.decode()


def test_container_is_removed_when_payload_parsing_raises(
    settings: PlanningSettings,
) -> None:
    container = _StubContainer(output=b"not JSON")
    docker_client = _StubDockerClient([container])

    with pytest.raises(PlanningTurnError):
        run_planning_turn(docker_client, settings, _request())

    assert container.removed is True


def test_failure_detail_leads_with_the_provider_error_not_the_prompt_echo(
    settings: PlanningSettings,
) -> None:
    """The CLI echoes its prompt, so a plain tail buries the reason."""
    output = "\n".join(
        [
            "Verify the plan against the project at /workspace. It is read-only.",
            "Cover expected behaviour including errors and edge cases.",
            "Review ledger: [] Prior findings are context, not truth.",
            "ERROR: Selected model is at capacity. Please try a different model.",
            "ERROR: Selected model is at capacity. Please try a different model.",
        ]
    ).encode()
    docker_client = _StubDockerClient([_StubContainer(output=output, status=1)])

    with pytest.raises(PlanningTurnError) as error:
        run_planning_turn(docker_client, settings, _request())

    detail = error.value.detail
    assert detail.endswith(
        "ERROR: Selected model is at capacity. Please try a different model."
    )
    # Prompt text is dropped, including a line that merely discusses errors.
    assert "Review ledger" not in detail
    assert "edge cases" not in detail
    # The provider printed the error twice; the reader needs it once.
    assert detail.count("at capacity") == 1
    # The untouched log stays on the exception for diagnosis.
    assert "Review ledger" in error.value.raw_output


def test_failure_detail_reports_an_empty_log_plainly(
    settings: PlanningSettings,
) -> None:
    docker_client = _StubDockerClient([_StubContainer(output=b"   \n\n", status=9)])

    with pytest.raises(PlanningTurnError) as error:
        run_planning_turn(docker_client, settings, _request())

    assert error.value.detail.endswith("the turn produced no output")
