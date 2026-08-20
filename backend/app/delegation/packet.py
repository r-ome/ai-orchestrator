"""Build the bounded prompt packet for one work item run."""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

MAX_ARCHITECTURE = 12
MAX_PATTERNS = 12
MAX_CONSTRAINTS = 10
MAX_UPSTREAM = 10


class ResolvedVerification(BaseModel):
    """A verification intent with its controller-confirmed command."""

    command_kind: str
    command: str
    reason: str = ""


class UpstreamResult(BaseModel):
    key: str
    title: str
    changed: list[str] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Packet(BaseModel):
    work_item_key: str
    title: str
    feature_summary: str
    objective: str
    scope: str
    out_of_scope: str = ""
    architecture: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification: list[ResolvedVerification] = Field(default_factory=list)
    upstream: list[UpstreamResult] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


def build_packet(
    *,
    item: Any,
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    commands: Mapping[str, str],
    upstream: Sequence[UpstreamResult],
) -> Packet:
    manifest = manifest or {}
    return Packet(
        work_item_key=item.key,
        title=item.title,
        feature_summary=_summary(plan),
        objective=item.objective,
        scope=item.scope,
        out_of_scope=item.out_of_scope,
        architecture=_merge(
            item.architecture,
            manifest.get("architecture"),
            MAX_ARCHITECTURE,
        ),
        patterns=_take(manifest.get("patterns"), MAX_PATTERNS),
        constraints=_take(manifest.get("constraints"), MAX_CONSTRAINTS),
        files=list(item.files),
        symbols=list(item.symbols),
        acceptance_criteria=list(item.acceptance_criteria),
        verification=[
            ResolvedVerification(
                command_kind=intent.command_kind,
                command=commands[intent.command_kind],
                reason=intent.reason,
            )
            for intent in item.verification
            if intent.command_kind in commands
        ],
        upstream=list(upstream)[:MAX_UPSTREAM],
        risks=list(item.risks),
    )


def render(packet: Packet) -> str:
    """Render a packet for a writable coding turn."""
    sections = [
        (
            "You are implementing one work item in a larger feature.\n\n"
            f"The feature, in short:\n{packet.feature_summary}"
        ),
        f"## What this item must do\n\n{packet.objective}",
        f"## In scope\n\n{packet.scope}",
    ]
    if packet.out_of_scope:
        sections.append(
            f"## Out of scope\n\n{packet.out_of_scope}\n\n"
            "Leave these alone even if they look wrong. Another item may own them."
        )
    if packet.upstream:
        sections.append(_upstream(packet.upstream))
    if packet.architecture:
        sections.append(_bullets("Decisions already made", packet.architecture))
    if packet.patterns:
        sections.append(
            _bullets("How this codebase does things", packet.patterns)
            + "\n\nMatch these patterns."
        )
    if packet.constraints:
        sections.append(_bullets("Constraints", packet.constraints))
    if packet.files or packet.symbols:
        sections.append(_where(packet))
    if packet.risks:
        sections.append(_bullets("Known risks in this item", packet.risks))
    sections.append(_bullets("Done when", packet.acceptance_criteria))
    if packet.verification:
        sections.append(_verification(packet.verification))
    return "\n\n".join(sections)


def _summary(plan: Mapping[str, Any]) -> str:
    scope = str(plan.get("scope") or "").strip()
    approach = str(plan.get("approach") or "").strip()
    return "\n\n".join(part for part in (scope, approach) if part) or "(not recorded)"


def _upstream(upstream: Sequence[UpstreamResult]) -> str:
    blocks = []
    for result in upstream:
        lines = [f"### {result.title}"]
        if result.changed:
            lines.append(_bullets("Changed", result.changed, level=0))
        if result.interfaces:
            lines.append(
                _bullets(
                    "Interfaces introduced or changed",
                    result.interfaces,
                    level=0,
                )
            )
        if result.notes:
            lines.append(_bullets("Notes for you", result.notes, level=0))
        blocks.append("\n\n".join(lines))
    return (
        "## What earlier items already did\n\n"
        "These items are merged. Build on them instead of repeating them.\n\n"
        + "\n\n".join(blocks)
    )


def _where(packet: Packet) -> str:
    lines = ["## Where to look"]
    if packet.files:
        lines.append(_bullets("Files", packet.files, level=0))
    if packet.symbols:
        lines.append(_bullets("Symbols", packet.symbols, level=0))
    lines.append(
        "Read these before you change anything. Current files are the source of truth."
    )
    return "\n\n".join(lines)


def _verification(verification: Sequence[ResolvedVerification]) -> str:
    listed = "\n".join(
        f"- `{entry.command}`" + (f" — {entry.reason}" if entry.reason else "")
        for entry in verification
    )
    return (
        "## Verification\n\n"
        "Run these commands before you report completion. Fix reported problems.\n\n"
        f"{listed}"
    )


def _bullets(heading: str, values: Sequence[str], *, level: int = 1) -> str:
    prefix = "#" * (level + 1)
    listed = "\n".join(f"- {value}" for value in values)
    return f"{prefix} {heading}\n\n{listed}" if level else f"**{heading}**\n\n{listed}"


def _merge(first: Sequence[str], second: Any, limit: int) -> list[str]:
    merged: list[str] = []
    for source in (first, second if isinstance(second, list) else []):
        for value in source:
            if isinstance(value, str) and value.strip() and value not in merged:
                merged.append(value)
    return merged[:limit]


def _take(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry.strip()][
        :limit
    ]
