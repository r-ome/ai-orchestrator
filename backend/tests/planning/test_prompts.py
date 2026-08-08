from app.agents.models import AgentProvider
from app.planning.config import PlanningSettings
from app.planning.runner import turn_model
from app.planning.prompts import (
    JSON_INSTRUCTION,
    clarifier_prompt,
    planner_prompt,
    reviewer_prompt,
)


def test_clarifier_prompt_contains_earlier_messages_in_order() -> None:
    prompt = clarifier_prompt(
        title="Export reports",
        messages=[
            {"role": "user", "text": "I need CSV exports."},
            {"role": "clarifier", "text": "Which reports?"},
            {"role": "user", "text": "The monthly report."},
        ],
    )

    first = prompt.index("I need CSV exports.")
    second = prompt.index("Which reports?")
    third = prompt.index("The monthly report.")
    assert first < second < third


def test_planner_prompt_only_includes_ledger_after_first_round() -> None:
    previous_turns = [{"role": "planner", "text": "First plan"}]
    ledger = [{"id": "F1", "severity": "major", "text": "Add rollback"}]

    first_round = planner_prompt(
        brief="The brief",
        round_number=1,
        previous_turns=previous_turns,
        ledger=ledger,
    )
    second_round = planner_prompt(
        brief="The brief",
        round_number=2,
        previous_turns=previous_turns,
        ledger=ledger,
    )

    assert "Review ledger" not in first_round
    assert "First plan" not in first_round
    assert "First plan" in second_round
    assert "F1" in second_round
    assert "Add rollback" in second_round
    # Round one has nothing to respond to. Offering the field anyway makes the
    # planner invent findings and answer them, and those ids then collide with
    # the reviewer's real ones.
    assert "finding_responses" not in first_round
    assert "finding_responses" in second_round


def test_reviewer_prompt_contains_ledger_but_not_planner_transcript() -> None:
    prompt = reviewer_prompt(
        brief="The brief",
        plan_markdown="# Current plan",
        ledger=[{"id": "F1", "severity": "major", "text": "Add rollback"}],
    )

    assert "F1" in prompt
    assert "Add rollback" in prompt
    assert "Your previous planning turns" not in prompt


def test_model_prompts_end_with_the_json_instruction() -> None:
    prompts = [
        clarifier_prompt(title="Title", messages=[]),
        planner_prompt(brief="Brief", round_number=1, previous_turns=[], ledger=[]),
        reviewer_prompt(brief="Brief", plan_markdown="Plan", ledger=[]),
    ]

    assert all(prompt.endswith(JSON_INSTRUCTION) for prompt in prompts)


def test_turn_model_names_the_model_each_provider_runs() -> None:
    settings = PlanningSettings(
        clarifier_provider=AgentProvider.CLAUDE,
        planner_provider=AgentProvider.CLAUDE,
        reviewer_provider=AgentProvider.CODEX,
        credential_profile="default",
        max_review_turns=3,
        turn_timeout_seconds=10,
        planning_memory="2g",
        claude_model="opus",
        codex_model="gpt-5.6-terra",
        codex_reasoning_effort="high",
    )

    assert turn_model(AgentProvider.CLAUDE, settings) == "opus"
    # Codex carries its reasoning effort: the same model at a different effort
    # is a different run and costs differently.
    assert turn_model(AgentProvider.CODEX, settings) == "gpt-5.6-terra (high effort)"
