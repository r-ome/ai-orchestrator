"""Coerce untrusted values into safe application values."""

import json
from typing import Any


def json_object(value: Any) -> dict[str, Any] | None:
    """Return a JSON object, or None for an invalid value."""
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def clamped_integer(value: Any) -> int:
    """Return a non-negative integer, or zero for an invalid value."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
