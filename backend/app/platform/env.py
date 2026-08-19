"""Read `backend/.env` into the process environment at import time.

Every settings module reads `os.getenv` with a code default, so the file only
has to land in `os.environ` before the first `get_*_settings()` call. Importing
this from `app/__init__.py` guarantees that: nothing in the package can be
imported without it running first.

A real environment variable always wins. The file supplies defaults for a local
deployment; a container or CI run overrides them the normal way.
"""

import os
from pathlib import Path

#: `backend/.env`, three levels up from this file.
ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


def integer_setting(name: str, default: int) -> int:
    """Return a positive integer setting, or its default value."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def load_env(path: Path | None = None) -> dict[str, str]:
    """Apply the file's assignments and return the ones that were applied.

    Missing file, blank lines, and `#` comments are all no-ops. A line without
    `=` is skipped rather than raised on: a malformed line should not stop the
    backend from starting.
    """
    source = ENV_FILE if path is None else path
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return {}

    applied: dict[str, str] = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        name, separator, value = entry.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name or name in os.environ:
            continue
        os.environ[name] = applied[name] = _unquote(value.strip())
    return applied


def _unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
