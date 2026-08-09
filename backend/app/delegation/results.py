"""Validate the structured result reported by a work item turn."""

from collections.abc import Mapping
from typing import Any


OUTCOMES = ("passed", "failed", "not_run")
MAX_ENTRY = 2000


def validate_result_payload(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    errors += _string_list(payload, "changed", required=True)
    for field in ("decisions", "interfaces", "notes_for_downstream"):
        errors += _string_list(payload, field, required=False)

    verification = payload.get("verification")
    if verification is None:
        errors.append("'verification' is required")
        return errors
    if not isinstance(verification, Mapping):
        errors.append("'verification' must be an object")
        return errors

    outcome = verification.get("outcome")
    if outcome not in OUTCOMES:
        errors.append(f"verification.outcome must be one of {list(OUTCOMES)}")
    errors += _string_list(
        verification,
        "ran",
        required=False,
        label="verification",
    )
    detail = verification.get("detail")
    if detail is not None and not isinstance(detail, str):
        errors.append("verification.detail must be a string")

    ran = verification.get("ran")
    if outcome in {"passed", "failed"} and isinstance(ran, list) and not ran:
        errors.append(
            f"verification.outcome is '{outcome}', so 'ran' must name the commands run"
        )
    return errors


def _string_list(
    payload: Mapping[str, Any],
    field: str,
    *,
    required: bool,
    label: str = "",
) -> list[str]:
    name = f"{label}.{field}" if label else f"'{field}'"
    value = payload.get(field)
    if value is None:
        return [f"{name} is required"] if required else []
    if not isinstance(value, list):
        return [f"{name} must be a list of strings"]
    if required and not value:
        return [f"{name} must not be empty"]
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        return [f"{name} must contain only non-empty strings"]
    if any(len(entry) > MAX_ENTRY for entry in value if isinstance(entry, str)):
        return [f"{name} entries must be shorter than {MAX_ENTRY} characters"]
    return []
