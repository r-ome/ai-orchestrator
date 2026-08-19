import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


def _ensure_immutable_manifest_value(
    *,
    field: str,
    existing: Any,
    requested: Any,
) -> None:
    if existing is not None and requested != existing:
        raise ValueError(f"sandbox manifest field {field!r} is immutable once set")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
