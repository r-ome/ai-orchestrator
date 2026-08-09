"""Prompt construction for the implementation-context turn."""

import json
from collections.abc import Sequence
from typing import Any

from app.implementation_context.inventory import CommandInventory
from app.implementation_context.models import COMMAND_KINDS


WORKSPACE = "/workspace"


def context_prompt(
    objective: str,
    plan: Any,
    available: Sequence[str],
    inventory: CommandInventory | None = None,
) -> str:
    return f"""You are gathering implementation context for a change that has already been planned.
The codebase is at {WORKSPACE}. Read it.

Whoever implements this will get your output instead of the repository. They will read the
files themselves. Tell them which files matter, what the important pieces are called, and what
this codebase expects. Point at things. Do not paste source code.

"modules": the files and directories this change will touch or must respect. Use real paths
relative to the repository root. Include each path's purpose.

"symbols": the functions, classes, types, or constants that matter. Give each name, location,
and role in this change.

"architecture": decisions that the implementation must not contradict.

"patterns": naming, error handling, testing, and module-layout conventions to follow.

"constraints": compatibility rules and dependencies that limit the implementation.

"commands": how this project builds, tests, lints, type-checks, and formats. Allowed keys are
{list(COMMAND_KINDS)}. Omit commands that do not exist. The controller runs every command you
name against the project's own manifests and drops the ones that do not match, so a guess costs
the implementer that command entirely. Work in this order and stop at the first that answers:

1. A command this project's CI already runs. CI is proof: it passes on a clean checkout.
2. A script or target the manifests define, invoked through this project's package manager.
3. Nothing. Omit the key.

Never adapt a command from a project that resembles this one. Never assume `npm` because the
project is JavaScript, or `pytest` because it is Python — use what the files below prove.
{_available(available)}{_evidence(inventory)}
"assumptions": facts you take for granted that could be wrong and affect implementation.

The request:
{objective}

The plan:
{json.dumps(plan, indent=2, sort_keys=True)}

Return exactly one JSON object. Do not add prose or a markdown fence.

{{
  "modules": [{{"path": "src/...", "purpose": "..."}}],
  "symbols": [{{"name": "...", "location": "src/...", "role": "..."}}],
  "architecture": ["..."],
  "patterns": ["..."],
  "constraints": ["..."],
  "commands": {{"test": "...", "lint": "..."}},
  "assumptions": ["..."]
}}"""


def _available(available: Sequence[str]) -> str:
    if not available:
        return "\n"
    listed = ", ".join(sorted(available))
    return (
        "\nThe controller already read this project's manifest files. "
        f"These commands are available: {listed}. Prefer them.\n"
    )


def _evidence(inventory: CommandInventory | None) -> str:
    """What the controller proved about this project, stated as fact.

    Read from the repository before the turn starts, so the turn does not spend
    tool calls rediscovering it and cannot misread it. Versions are here because
    the turn knows these libraries but not which release this project pins, and
    that is what decides whether the API it recommends exists.
    """
    if inventory is None:
        return ""
    sections: list[str] = []

    if inventory.package_manager:
        sections.append(
            f"This project's package manager is {inventory.package_manager}. Its "
            f"lockfile proves it. Any command you give for a package script must "
            f"start with {inventory.package_manager}."
        )
    if inventory.ci_commands:
        listed = "\n".join(f"  {command}" for command in inventory.ci_commands)
        sections.append("This project's CI runs these commands:\n" + listed)
    if inventory.dependencies:
        listed = "\n".join(
            f"  {name} {version}".rstrip() for name, version in inventory.dependencies
        )
        sections.append(
            "This project depends on these versions. Your architecture, patterns "
            "and constraints must hold for these releases, not for the newest one "
            "you know of. If a version is old enough that an API you would "
            "recommend does not exist in it, say so in constraints:\n" + listed
        )

    if not sections:
        return ""
    return "\nWhat the controller already established:\n\n" + "\n\n".join(sections) + "\n"


def repair_prompt(
    original: str,
    errors: Sequence[str],
    raw_output: str,
) -> str:
    listed = "\n".join(f"- {error}" for error in errors)
    return "\n\n".join(
        [
            original,
            "Your previous reply could not be accepted:\n" + listed,
            "Previous reply:\n" + raw_output[:4000],
            "Read the relevant files again. Reply with one corrected JSON object only.",
        ]
    )
