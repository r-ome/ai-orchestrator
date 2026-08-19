"""Prompt construction for the Delegator role."""

import json
from collections.abc import Sequence
from typing import Any

WORKSPACE = "/workspace"


def delegator_prompt(
    objective: str,
    plan: Any,
    manifest: Any,
    command_kinds: Sequence[str],
) -> str:
    return f"""You are the Delegator. Break the plan below into work items that can be
implemented one at a time.

A work item is the smallest independently meaningful unit of work. One run must implement,
verify, and report it without making architectural decisions.

Avoid items that are too large. "Implement authentication" is a project, not a work item.
Avoid items that are too small. A field, serializer change, and test are one item when they
form one behavior. The right size is usually one behavior, end to end, including its test.

Dependencies are hard ordering only. Item B depends on item A only when B cannot exist until A
exists. Do not add dependencies because items touch the same file. That is a write conflict.

For each item:

- "key": a unique, lowercase, hyphenated identifier.
- "title": a short description.
- "objective": what must be true when the item is done.
- "scope" and "out_of_scope": the item boundary.
- "dependencies": keys of items that must finish first.
- "files" and "symbols": pointers from the implementation context.
- "write_scope": files that the item is expected to change.
- "acceptance_criteria": observable completion checks. Include at least one.
- "verification": required command kinds. Include at least one intent. Allowed kinds are
  {list(command_kinds)}. Do not use any other kind.
- "complexity": "low", "medium", or "high" reasoning difficulty.
- "architecture": decisions that the item must respect.
- "risks": item-specific failure risks.

The implementation context already identifies relevant code. Read {WORKSPACE} only to check a
specific unanswered fact. Do not survey the repository again.

Keep architectural decisions upstream. Record an undecided architectural issue as a risk.

The request:
{objective}

The plan:
{json.dumps(plan, indent=2, sort_keys=True)}

The implementation context:
{json.dumps(manifest, indent=2, sort_keys=True)}

Return exactly one JSON object. Do not add prose or a markdown fence.

{{
  "items": [
    {{
      "key": "add-reading-time-utility",
      "title": "...",
      "objective": "...",
      "scope": "...",
      "out_of_scope": "...",
      "dependencies": [],
      "files": ["src/..."],
      "symbols": ["..."],
      "write_scope": ["src/..."],
      "acceptance_criteria": ["..."],
      "verification": [
        {{"command_kind": "{command_kinds[0] if command_kinds else 'build'}", "reason": "..."}}
      ],
      "complexity": "low",
      "architecture": ["..."],
      "risks": ["..."]
    }}
  ]
}}"""
