import json
from types import SimpleNamespace
from typing import Any

import pytest
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
    run_turn_with_repair,
)


class _StubContainer:
    def __init__(
        self,
        *,
        output: bytes = b'{"message": "ok"}',
        status: int = 0,
        wait_error: Exception | None = None,
    ) -> None:
        self.output = output
        self.status = status
        self.wait_error = wait_error
        self.started = False
        self.killed = False
        self.removed = False

    def start(self) -> None:
        self.started = True

    def wait(self, *, timeout: int) -> dict[str, int]:
        if self.wait_error is not None:
            raise self.wait_error
        return {"StatusCode": self.status}

    def logs(self) -> bytes:
        return self.output

    def kill(self) -> None:
        self.killed = True

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

    monkeypatch.setattr(runner, "get_agent_settings", lambda: SimpleNamespace(provider=provider))
    monkeypatch.setattr(runner, "credential_volume", lambda *_: SimpleNamespace(name="auth-volume"))


def _request(provider: AgentProvider = AgentProvider.CODEX) -> TurnRequest:
    return TurnRequest(
        role=PlanningRole.CLARIFIER,
        provider=provider,
        prompt="The prompt must only be in the environment.",
        project_volume="project-volume",
        session_id="session-1",
    )


def test_claude_command_has_read_only_planning_flags(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient([_StubContainer(output=b'{"result": "{\\"message\\": \\"ok\\"}"}')])

    run_planning_turn(docker_client, settings, _request(AgentProvider.CLAUDE))

    command = docker_client.containers.create_calls[0]["command"][2]
    assert "--permission-mode plan" in command
    assert "--allowedTools" in command
    assert "--output-format json" in command


def test_codex_command_ends_exec_with_closed_stdin(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient([_StubContainer()])

    run_planning_turn(docker_client, settings, _request())

    command = docker_client.containers.create_calls[0]["command"][2]
    assert "--sandbox read-only" in command
    assert "< /dev/null && cat /tmp/planning-output.json" in command


def test_container_mounts_are_read_only_for_the_project(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient([_StubContainer()])

    run_planning_turn(docker_client, settings, _request())

    create_call = docker_client.containers.create_calls[0]
    assert create_call["volumes"] == {
        "project-volume": {"bind": "/workspace", "mode": "ro"},
        "auth-volume": {"bind": "/auth", "mode": "rw"},
    }
    assert "network_disabled" not in create_call


def test_prompt_is_in_the_environment_not_the_command(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient([_StubContainer()])
    request = _request()

    run_planning_turn(docker_client, settings, request)

    create_call = docker_client.containers.create_calls[0]
    assert create_call["environment"][PROMPT_VARIABLE] == request.prompt
    assert request.prompt not in create_call["command"][2]


def test_extract_payload_accepts_prose_around_json() -> None:
    payload = extract_payload(
        "Here is the reply. {\"message\": \"ok\", \"questions\": []} Thank you.",
        provider=AgentProvider.CODEX,
    )

    assert payload == {"message": "ok", "questions": []}


def test_extract_payload_ignores_braces_and_escapes_inside_strings() -> None:
    expected = {
        "plan_markdown": "Use {value} and the literal \\\"}\\\" without ending JSON.",
        "scope": "Includes {braces}",
    }
    raw = "Model preamble: " + json.dumps(expected) + " trailing prose"

    assert extract_payload(raw, provider=AgentProvider.CODEX) == expected


def test_malformed_output_runs_exactly_one_repair_turn(settings: PlanningSettings) -> None:
    first = _StubContainer(output=b"not JSON")
    second = _StubContainer(output=b'{"message": "repaired"}')
    docker_client = _StubDockerClient([first, second])

    result = run_turn_with_repair(docker_client, settings, _request(), lambda _: None)

    assert result.payload == {"message": "repaired"}
    assert len(docker_client.containers.create_calls) == 2
    repair_prompt = docker_client.containers.create_calls[1]["environment"][PROMPT_VARIABLE]
    assert "Your previous reply could not be used." in repair_prompt


def test_second_malformed_output_raises(settings: PlanningSettings) -> None:
    docker_client = _StubDockerClient([_StubContainer(output=b"bad"), _StubContainer(output=b"still bad")])

    with pytest.raises(PlanningTurnError) as error:
        run_turn_with_repair(docker_client, settings, _request(), lambda _: None)

    assert error.value.status_code == 422
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


def test_container_is_removed_when_payload_parsing_raises(settings: PlanningSettings) -> None:
    container = _StubContainer(output=b"not JSON")
    docker_client = _StubDockerClient([container])

    with pytest.raises(PlanningTurnError):
        run_planning_turn(docker_client, settings, _request())

    assert container.removed is True
