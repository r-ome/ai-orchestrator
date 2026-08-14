import json
from collections.abc import Mapping, Sequence
from typing import Any


CLARIFIER_SCHEMA = """{
  \"message\": \"one short paragraph to the human\",
  \"questions\": [\"two or three, each one sentence; never more than three\"],
  \"ready_to_summarize\": false,
  \"understanding_summary\": \"\"
}"""

def _render_prose(value: Any) -> str:
    text = str(value or "").strip()
    return text or "(none)"


def _render_components(value: Any) -> str:
    items = value if isinstance(value, list) else []
    lines = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        responsibility = str(item.get("responsibility", "")).strip()
        lines.append(f"- {name}: {responsibility}" if responsibility else f"- {name}")
    return "\n".join(lines) or "(none)"


def _render_risks(value: Any) -> str:
    items = value if isinstance(value, list) else []
    lines = [
        f"- [{str(item.get('severity', '')).strip()}] {str(item.get('text', '')).strip()}"
        for item in items
        if isinstance(item, Mapping)
    ]
    return "\n".join(lines) or "(none)"


def _render_questions(value: Any) -> str:
    items = value if isinstance(value, list) else []
    lines = [f"{index}. {str(item).strip()}" for index, item in enumerate(items, start=1)]
    return "\n".join(lines) or "(none)"


# One list drives both halves of the plan contract: the schema the planner is
# asked to fill, and the rendering the reviewer is shown. They have to stay
# together. When the reviewer received `plan_markdown` alone, a planner that
# put a scope decision in `open_questions` and pointed at it from the markdown
# produced a plan the reviewer could not read whole: it saw the reference, saw
# no such section, and re-raised a finding the planner had already answered.
# The loop ran to its turn limit on a disagreement neither side could settle.
# So the invariant is: anything the planner can put a claim in must reach the
# reviewer. Adding a field here without a renderer fails the coverage test in
# tests/planning/test_prompts.py.
_PLAN_FIELDS: tuple[tuple[str, str, str, Any], ...] = (
    ("plan_markdown", '"the full plan as markdown"', "Plan", _render_prose),
    ("scope", '"what this feature includes and excludes"', "Scope", _render_prose),
    ("approach", '"the proposed approach in prose"', "Approach", _render_prose),
    (
        "components",
        '[{"name": "...", "responsibility": "..."}]',
        "Components",
        _render_components,
    ),
    ("risks", '[{"severity": "high|medium|low", "text": "..."}]', "Risks", _render_risks),
    ("open_questions", '["..."]', "Open questions", _render_questions),
)

_PLANNER_SCHEMA_BODY = ",\n".join(
    f'  "{name}": {literal}' for name, literal, _, _ in _PLAN_FIELDS
)


def render_plan(plan: Mapping[str, Any]) -> str:
    """Render every planner field the reviewer has to judge.

    Empty fields render as `(none)` rather than disappearing. A missing
    section reads to the reviewer as a plan it was handed incompletely; an
    explicit `(none)` reads as a planner that had nothing to say.
    """
    return "\n\n".join(
        f"## {heading}\n{render(plan.get(name))}"
        for name, _, heading, render in _PLAN_FIELDS
    )

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
        # Without this the planner treats plan_markdown as the only thing that is
        # read, and puts the decision that settles a finding somewhere else.
        (
            "The reviewer receives every field of your reply, not plan_markdown "
            "alone. Put each thing in its own field: an unresolved decision belongs "
            "in open_questions, not buried in the markdown, and the markdown should "
            "not point at a section it does not contain."
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
    plan: Mapping[str, Any],
    ledger: Sequence[Mapping[str, Any]],
) -> str:
    return "\n\n".join(
        [
            "You are the plan reviewer for a project-level planning session.",
            f"Feature brief:\n{brief}",
            (
                "Current plan. This is the whole plan the planner produced, every "
                "field of it. Judge all of it, not the Plan section alone:\n"
                + render_plan(plan)
            ),
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
    """Render one conversation, excluding operator diagnostics.

    System entries are written for the person reading the session: a failed
    turn's raw container log, and retry notices. They are not conversation, and
    a raw log carries an echo of an earlier prompt, so feeding them back would
    put stale instructions inside the next turn's prompt.
    """
    conversation = [
        message
        for message in messages
        if str(message.get("role", "")).lower() != "system"
    ]
    if not conversation:
        return "(none)"
    return "\n".join(
        f"{str(message.get('role', 'unknown')).upper()}: {message.get('text', '')}"
        for message in conversation
    )


def _render_json(value: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(value), indent=2, ensure_ascii=False)
