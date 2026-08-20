import json

from app.agents.models import AgentProvider
from app.agents.output import extract_payload


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
