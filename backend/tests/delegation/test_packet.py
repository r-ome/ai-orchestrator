from types import SimpleNamespace
from typing import Any

from app.delegation.packet import UpstreamResult, build_packet, render
from app.delegation.results import validate_result_payload


PLAN = {
    "scope": "Add tags to posts",
    "approach": "Schema, helpers, then pages",
    "plan_markdown": "# Plan\n\nfull detail",
    "components": [{"name": "schema", "responsibility": "declares the field"}],
}
MANIFEST = {
    "architecture": ["content lives under src/content"],
    "patterns": ["utilities are exported functions in src/utils"],
    "constraints": ["Astro 5"],
    "modules": [{"path": "src/x.ts", "purpose": "not copied into the packet"}],
}


def _item(**overrides: Any) -> SimpleNamespace:
    item = SimpleNamespace(
        key="add-tag-helpers",
        title="Add tag helpers",
        objective="Collect and count tags across posts",
        scope="src/utils/tags.ts only",
        out_of_scope="the pages that consume it",
        files=["src/utils/tags.ts", "src/content.config.ts"],
        symbols=["collectTags"],
        acceptance_criteria=["collectTags returns counts per tag"],
        verification=[
            SimpleNamespace(command_kind="build", reason="it compiles"),
            SimpleNamespace(command_kind="test", reason="not confirmed"),
        ],
        architecture=["tags are optional on every post"],
        risks=["posts without tags"],
    )
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def _packet(**overrides: Any):
    values: dict[str, Any] = {
        "item": _item(),
        "plan": PLAN,
        "manifest": MANIFEST,
        "commands": {"build": "npm run build"},
        "upstream": [],
    }
    values.update(overrides)
    return build_packet(**values)


def test_packet_is_bounded_and_resolves_only_confirmed_commands() -> None:
    packet = _packet()

    assert packet.feature_summary == "Add tags to posts\n\nSchema, helpers, then pages"
    assert "full detail" not in packet.feature_summary
    assert packet.architecture == [
        "tags are optional on every post",
        "content lives under src/content",
    ]
    assert [entry.command for entry in packet.verification] == ["npm run build"]


def test_render_includes_scope_current_file_guidance_and_verification() -> None:
    rendered = render(_packet())

    assert "src/utils/tags.ts only" in rendered
    assert "the pages that consume it" in rendered
    assert "Current files are the source of truth" in rendered
    assert "`npm run build`" in rendered
    assert "# Plan" not in rendered


def test_render_includes_upstream_results() -> None:
    rendered = render(
        _packet(
            upstream=[
                UpstreamResult(
                    key="schema",
                    title="Add tags field",
                    changed=["added tags to the schema"],
                    interfaces=["BlogPost.data.tags"],
                    notes=["existing posts have no tags"],
                )
            ]
        )
    )

    assert "What earlier items already did" in rendered
    assert "BlogPost.data.tags" in rendered
    assert "existing posts have no tags" in rendered


def test_valid_result_payload_passes() -> None:
    assert validate_result_payload(
        {
            "changed": ["added collectTags"],
            "decisions": ["counted case-insensitively"],
            "interfaces": ["collectTags(posts)"],
            "verification": {
                "ran": ["npm run build"],
                "outcome": "passed",
                "detail": "clean",
            },
            "notes_for_downstream": ["tags are lowercased"],
        }
    ) == []


def test_result_requires_changes_and_verification() -> None:
    errors = validate_result_payload({"changed": []})

    assert "'changed' must not be empty" in errors
    assert "'verification' is required" in errors


def test_passed_result_names_commands() -> None:
    errors = validate_result_payload(
        {"changed": ["x"], "verification": {"outcome": "passed", "ran": []}}
    )

    assert any("must name the commands run" in error for error in errors)


def test_not_run_result_needs_no_commands() -> None:
    assert validate_result_payload(
        {"changed": ["x"], "verification": {"outcome": "not_run", "ran": []}}
    ) == []
