import json
from collections.abc import Mapping, Sequence
from typing import Any


CLARIFIER_SCHEMA = """{
  \"message\": \"one short paragraph to the human\",
  \"questions\": [\"two or three, each one sentence; never more than three\"],
  \"ready_to_summarize\": false,
  \"understanding_summary\": \"\"
}"""

_PLANNER_SCHEMA_BODY = """  \"plan_markdown\": \"the full plan as markdown\",
  \"scope\": \"what this feature includes and excludes\",
  \"approach\": \"the proposed approach in prose\",
  \"components\": [{\"name\": \"...\", \"responsibility\": \"...\"}],
  \"risks\": [{\"severity\": \"high|medium|low\", \"text\": \"...\"}],
  \"open_questions\": [\"...\"]"""

# Round one has no review ledger, so the planner has nothing to respond to.
# Showing it the `finding_responses` field anyway invites it to invent findings
# and answer them, and those invented ids then collide with the real ones the
# reviewer raises immediately afterwards.
PLANNER_SCHEMA_FIRST_ROUND = "{\n" + _PLANNER_SCHEMA_BODY + "\n}"

PLANNER_SCHEMA = (
    "{\n"
    + _PLANNER_SCHEMA_BODY
    + """,
  \"finding_responses\": [
    {\"finding_id\": \"F1\", \"status\": \"answered|rejected\", \"rationale\": \"...\"}
  ]
}"""
)

REVIEWER_SCHEMA = """{
  \"approved\": false,
  \"summary\": \"one paragraph verdict\",
  \"findings\": [
    {\"id\": \"F1\", \"severity\": \"blocking|major|minor\", \"text\": \"...\"}
  ]
}"""

JSON_INSTRUCTION = "Reply with one JSON object and nothing else."


def clarifier_prompt(*, title: str, messages: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(
        [
            "You are the clarifier for a project-level planning session.",
            f"Feature title: {title}",
            "You may read the project at /workspace. It is read-only.",
            (
                "Do not produce a plan, an implementation, or a design. Ask two or "
                "three questions per reply whenever more than one thing is genuinely "
                "open. Ask a single question only when one answer must come first."
            ),
            (
                "Batch independent questions into one reply. Hold back only a question "
                "whose wording depends on another answer."
            ),
            (
                "Cover these areas across the conversation, not as a questionnaire: "
                "the desired outcome, scope in and out, constraints, expected behaviour "
                "including errors and edge cases, and the trade-offs that matter."
            ),
            (
                "When the feature is understood, set ready_to_summarize to true, ask no "
                "more questions, and put the full understanding in understanding_summary."
            ),
            "Conversation, oldest first:\n" + _render_messages(messages),
            "JSON schema:\n" + CLARIFIER_SCHEMA,
            JSON_INSTRUCTION,
        ]
    )


def planner_prompt(
    *,
    brief: str,
    round_number: int,
    previous_turns: Sequence[Mapping[str, Any]],
    ledger: Sequence[Mapping[str, Any]],
) -> str:
    sections = [
        "You are the planner for a project-level planning session.",
        "Plan only. Do not write code, write files, create a tech spec, or create a task breakdown.",
        f"Feature brief:\n{brief}",
        # Without this the first-round plan is written blind, and the review loop
        # becomes the channel that carries repository facts into it — one full
        # planner/reviewer round trip per fact, with the reviewer's mistakes
        # copied along the way.
        (
            "Read the project at /workspace before you plan. It is read-only. "
            "Name real paths, real symbols, and the conventions this repository "
            "actually follows. Do not defer reading to the implementer, and do not "
            "write a step whose content is 'inspect the existing code'."
        ),
        (
            "State a repository fact only if you read it. Do not claim to have run, "
            "compiled, built, or tested anything. Take dependency versions from the "
            "manifest or lockfile and name the file you took them from."
        ),
    ]
    first_round = round_number < 2
    if not first_round:
        sections.append("Your previous planning turns, oldest first:\n" + _render_messages(previous_turns))
        sections.append("Review ledger:\n" + _render_json(ledger))
        sections.append(
            "Respond to every finding in the review ledger, and to no other finding. "
            "Do not invent findings."
        )
    schema = PLANNER_SCHEMA_FIRST_ROUND if first_round else PLANNER_SCHEMA
    sections.extend(["JSON schema:\n" + schema, JSON_INSTRUCTION])
    return "\n\n".join(sections)


def reviewer_prompt(
    *,
    brief: str,
    plan_markdown: str,
    ledger: Sequence[Mapping[str, Any]],
) -> str:
    return "\n\n".join(
        [
            "You are the plan reviewer for a project-level planning session.",
            f"Feature brief:\n{brief}",
            f"Current plan:\n{plan_markdown}",
            (
                "Verify the plan against the project at /workspace. It is read-only. "
                "Check that the files, symbols, and conventions the plan names exist "
                "and behave as it assumes, before you raise or dismiss a finding."
            ),
            # A reviewer that reasons about what it cannot observe states the
            # conclusion with an invented method attached. Reading is reliable;
            # claimed execution is not.
            (
                "Cite a repository fact only if you read it, and give the path and line "
                "range. Do not claim to have run, compiled, built, or tested anything. "
                "Take dependency versions from the manifest or lockfile and name the "
                "file you took them from. Do not generalise across a directory you did "
                "not list."
            ),
            "Review ledger:\n" + _render_json(ledger),
            (
                "Prior findings are context, not truth. Assess the current plan from "
                "scratch. Do not reopen an answered finding without a concrete reason "
                "drawn from the current plan."
            ),
            (
                "Either accept a rejected finding's rationale or say precisely why it "
                "does not hold. Name every remaining issue and every newly introduced issue."
            ),
            (
                "Reuse a ledger id when re-raising a known finding. Use NEW-1, NEW-2, "
                "and so on for new findings."
            ),
            "JSON schema:\n" + REVIEWER_SCHEMA,
            JSON_INSTRUCTION,
        ]
    )


def feature_brief(
    *,
    title: str,
    request: str,
    understanding: str,
    messages: Sequence[Mapping[str, Any]],
    confirmed: bool,
) -> str:
    confirmation = "confirmed" if confirmed else "not confirmed; the human proceeded anyway"
    return "\n\n".join(
        [
            f"# Feature brief: {title}",
            f"## Original request\n{request}",
            f"## Understanding ({confirmation})\n{understanding}",
            "## Clarification conversation\n" + _render_messages(messages),
        ]
    )


def _render_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    if not messages:
        return "(none)"
    return "\n".join(
        f"{str(message.get('role', 'unknown')).upper()}: {message.get('text', '')}"
        for message in messages
    )


def _render_json(value: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(value), indent=2, ensure_ascii=False)
