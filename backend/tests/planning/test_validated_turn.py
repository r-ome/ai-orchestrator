from collections import deque

import pytest

from app.planning.runner import PlanningTurnError, TurnResult, run_validated_turn


def _result(payload: dict[str, object], raw_output: str = "reply") -> TurnResult:
    return TurnResult(raw_output=raw_output, payload=payload, model="test-model")


def test_accepts_a_valid_first_attempt() -> None:
    prompts: list[str] = []

    def run(prompt: str) -> TurnResult:
        prompts.append(prompt)
        return _result({"valid": True})

    outcome = run_validated_turn(run, prompt="original", validate=lambda _: [])

    assert outcome.accepted
    assert outcome.attempts == 1
    assert len(prompts) == 1


def test_repairs_an_invalid_payload_once() -> None:
    queue = deque([_result({"valid": False}), _result({"valid": True})])
    prompts: list[str] = []

    def run(prompt: str) -> TurnResult:
        prompts.append(prompt)
        return queue.popleft()

    outcome = run_validated_turn(
        run,
        prompt="original",
        validate=lambda payload: [] if payload["valid"] else ["not valid"],
    )

    assert outcome.accepted
    assert outcome.attempts == 2
    assert len(prompts) == 2


def test_rejects_two_invalid_payloads() -> None:
    queue = deque([_result({}), _result({})])

    outcome = run_validated_turn(
        lambda _: queue.popleft(),
        prompt="original",
        validate=lambda _: ["invalid"],
    )

    assert not outcome.accepted
    assert outcome.attempts == 2
    assert outcome.errors


def test_repair_prompt_lists_every_validation_error() -> None:
    queue = deque([_result({}), _result({"valid": True})])
    prompts: list[str] = []

    def run(prompt: str) -> TurnResult:
        prompts.append(prompt)
        return queue.popleft()

    run_validated_turn(
        run,
        prompt="original",
        validate=lambda payload: [] if payload.get("valid") else ["first error", "second error"],
    )

    assert "- first error" in prompts[1]
    assert "- second error" in prompts[1]


def test_repair_prompt_uses_the_previous_output_tail() -> None:
    raw = "early-prefix-marker" + "x" * 8000 + "tail-marker"
    queue = deque([_result({}, raw), _result({"valid": True})])
    prompts: list[str] = []

    def run(prompt: str) -> TurnResult:
        prompts.append(prompt)
        return queue.popleft()

    run_validated_turn(
        run,
        prompt="original",
        validate=lambda payload: [] if payload.get("valid") else ["invalid"],
    )

    assert "tail-marker" in prompts[1]
    assert "early-prefix-marker" not in prompts[1]


def test_returns_only_the_final_attempt_errors() -> None:
    queue = deque([_result({"error": "X"}), _result({"error": "Z"})])

    outcome = run_validated_turn(
        lambda _: queue.popleft(),
        prompt="original",
        validate=lambda payload: [str(payload["error"])],
    )

    assert outcome.errors == ["Z"]
    assert "X" not in outcome.errors


def test_propagates_non_422_turn_errors() -> None:
    def run(_: str) -> TurnResult:
        raise PlanningTurnError(502, "container failed")

    with pytest.raises(PlanningTurnError, match="container failed"):
        run_validated_turn(run, prompt="original", validate=lambda _: [])


def test_absorbs_422_turn_errors() -> None:
    def run(_: str) -> TurnResult:
        raise PlanningTurnError(422, "bad JSON", "raw")

    outcome = run_validated_turn(run, prompt="original", validate=lambda _: [])

    assert outcome.result is None
    assert outcome.errors == ["bad JSON"]
    assert outcome.attempts == 2
