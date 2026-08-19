"""Deterministic validation for implementation-context payloads."""

from collections.abc import Mapping
from typing import Any

from app.implementation_context.models import COMMAND_KINDS

MAX_MODULES = 60
MAX_SYMBOLS = 80
MAX_EXCERPT = 2000


def validate_context_payload(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    errors += _objects(payload, "modules", ("path", "purpose"), MAX_MODULES, required=True)
    errors += _objects(
        payload,
        "symbols",
        ("name", "location", "role"),
        MAX_SYMBOLS,
        required=False,
    )
    for field in ("architecture", "patterns", "constraints", "assumptions"):
        errors += _string_list(payload, field)

    commands = payload.get("commands")
    if not isinstance(commands, Mapping):
        errors.append("'commands' is required and must be an object")
        return errors

    unknown = sorted(set(commands) - set(COMMAND_KINDS))
    if unknown:
        errors.append(
            f"'commands' has unknown keys {unknown}; "
            f"allowed keys are {list(COMMAND_KINDS)}"
        )
    for kind, command in commands.items():
        if command is None:
            continue
        if not isinstance(command, str) or not command.strip():
            errors.append(f"commands.{kind} must be a non-empty string or omitted")
        elif "\n" in command:
            errors.append(f"commands.{kind} must be a single command, not several lines")

    errors += _excerpts(payload)
    return errors


def _excerpts(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("architecture", "patterns", "constraints", "assumptions"):
        values = payload.get(field)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            if isinstance(value, str) and len(value) > MAX_EXCERPT:
                errors.append(
                    f"{field}[{index}] is longer than {MAX_EXCERPT} characters; "
                    "record where to look, not the contents"
                )
    return errors


def _objects(
    payload: Mapping[str, Any],
    field: str,
    keys: tuple[str, ...],
    limit: int,
    *,
    required: bool,
) -> list[str]:
    value = payload.get(field)
    if value is None:
        return [f"'{field}' is required and must be a non-empty list"] if required else []
    if not isinstance(value, list):
        return [f"'{field}' must be a list of objects"]
    if required and not value:
        return [f"'{field}' must not be empty"]
    if len(value) > limit:
        return [f"'{field}' must contain at most {limit} entries"]

    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"{field}[{index}] must be an object with {list(keys)}")
            continue
        for key in keys:
            entry = item.get(key)
            if not isinstance(entry, str) or not entry.strip():
                errors.append(f"{field}[{index}].{key} must be a non-empty string")
    return errors


def _string_list(payload: Mapping[str, Any], field: str) -> list[str]:
    value = payload.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"'{field}' must be a list of strings"]
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return [f"'{field}' must contain only non-empty strings"]
    return []
