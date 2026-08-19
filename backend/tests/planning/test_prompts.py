from app.agents.models import AgentProvider
from app.planning.config import PlanningSettings
from app.planning.prompts import (
    _PLAN_FIELDS,
    JSON_INSTRUCTION,
    PLANNER_SCHEMA,
    clarifier_prompt,
    planner_prompt,
    render_plan,
    reviewer_prompt,
)
from app.planning.runner import turn_model


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
        plan={"plan_markdown": "# Current plan"},
        ledger=[{"id": "F1", "severity": "major", "text": "Add rollback"}],
    )

    assert "F1" in prompt
    assert "Add rollback" in prompt
    assert "Your previous planning turns" not in prompt


def test_reviewer_prompt_shows_every_planner_field() -> None:
    """The planner must not be able to justify a finding out of the reviewer's sight.

    A plan whose only visible field was plan_markdown let the planner answer a
    blocking finding in open_questions. The reviewer re-raised it every round
    because it could not see the answer, and the loop hit its turn limit.
    """
    plan = {
        "plan_markdown": "# Plan\nSee Open question 1.",
        "scope": "One file only",
        "approach": "Edit the footer component",
        "components": [{"name": "Footer.astro", "responsibility": "renders the icons"}],
        "risks": [{"severity": "medium", "text": "The footer is mounted nowhere"}],
        "open_questions": ["Should the footer be mounted at all?"],
    }

    prompt = reviewer_prompt(brief="The brief", plan=plan, ledger=[])

    assert "One file only" in prompt
    assert "Edit the footer component" in prompt
    assert "Footer.astro: renders the icons" in prompt
    assert "[medium] The footer is mounted nowhere" in prompt
    assert "1. Should the footer be mounted at all?" in prompt


def test_every_planner_schema_field_reaches_the_reviewer() -> None:
    """Adding a planner field without rendering it recreates the side channel."""
    rendered = render_plan({})
    for name, _, heading, _ in _PLAN_FIELDS:
        assert f'"{name}"' in PLANNER_SCHEMA, (
            f"{name} is missing from the planner schema"
        )
        assert f"## {heading}" in rendered, f"{name} never reaches the reviewer"


def test_absent_planner_fields_render_as_none_rather_than_vanishing() -> None:
    rendered = render_plan({})

    assert "## Open questions\n(none)" in rendered
    assert "## Risks\n(none)" in rendered


def test_model_prompts_end_with_the_json_instruction() -> None:
    prompts = [
        clarifier_prompt(title="Title", messages=[]),
        planner_prompt(brief="Brief", round_number=1, previous_turns=[], ledger=[]),
        reviewer_prompt(brief="Brief", plan={"plan_markdown": "Plan"}, ledger=[]),
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


def test_conversation_prompts_exclude_operator_system_messages() -> None:
    """A failed turn's raw log is written for the reader, not for the next turn.

    That log contains an echo of an earlier prompt, so rendering it back would
    put stale instructions inside the next turn's own prompt.
    """
    messages = [
        {"role": "user", "text": "Add an article action bar."},
        {"role": "clarifier", "text": "Which pages should it appear on?"},
        {
            "role": "system",
            "text": "reviewer turn exited with status 1: Review ledger: [] "
            "Prior findings are context. ERROR: Selected model is at capacity.",
        },
    ]

    prompt = clarifier_prompt(title="action bar", messages=messages)

    assert "Which pages should it appear on?" in prompt
    assert "at capacity" not in prompt
    # An echoed instruction from the reviewer's prompt, which the clarifier's
    # own boilerplate never contains.
    assert "Review ledger:" not in prompt


def test_conversation_prompt_reports_no_conversation_when_only_system_remains() -> None:
    prompt = clarifier_prompt(
        title="action bar",
        messages=[{"role": "system", "text": "turn exited with status 1"}],
    )

    assert "Conversation, oldest first:\n(none)" in prompt
