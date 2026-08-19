"""Versioned fingerprints for uncommitted Git worktree entries."""

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

SNAPSHOT_VERSION = 1
_MARKER = f"snapshot-version {SNAPSHOT_VERSION}"


@dataclass(frozen=True, order=True)
class DirtyEntry:
    """One path reported by Git plus its current filesystem identity."""

    path: str
    status: str
    file_type: str
    fingerprint: str | None = None


def snapshot_shell() -> str:
    """Return a POSIX shell fragment that emits a NUL-safe dirty snapshot."""
    return (
        f'printf "{_MARKER}\\n"\n'
        "git status --porcelain=v1 -z --untracked-files=all | "
        "while IFS= read -r -d '' entry; do\n"
        '  status="$(printf "%.2s" "$entry")"\n'
        '  path="${entry#???}"\n'
        "  case \"$status\" in R*|C*) IFS= read -r -d '' _source || true;; esac\n"
        '  display="./$path"\n'
        '  fingerprint="-"\n'
        '  if [ -L "$display" ]; then\n'
        '    file_type="symlink"\n'
        '    fingerprint="sha256:$(readlink -n "$display" | sha256sum | awk \'{print $1}\')"\n'
        '  elif [ -d "$display" ]; then\n'
        '    file_type="directory"\n'
        '  elif [ -e "$display" ]; then\n'
        '    case "$(stat -c %F "$display")" in\n'
        '      "regular file")\n'
        '        file_type="file"\n'
        '        fingerprint="sha256:$(sha256sum "$display" | awk \'{print $1}\')"\n'
        "        ;;\n"
        '      *) file_type="other";;\n'
        "    esac\n"
        "  else\n"
        '    file_type="missing"\n'
        "  fi\n"
        '  status64="$(printf "%s" "$status" | base64 | tr -d "\\n")"\n'
        '  path64="$(printf "%s" "$path" | base64 | tr -d "\\n")"\n'
        '  printf "snapshot %s %s %s %s\\n" "$status64" "$file_type" '
        '"$fingerprint" "$path64"\n'
        '  printf "dirty %s\\n" "$entry"\n'
        "done\n"
    )


def parse_snapshot(output: bytes) -> list[DirtyEntry] | None:
    """Parse shell output, or return None for callers that predate snapshots."""
    text = output.decode("utf-8", errors="replace")
    if _MARKER not in text.splitlines():
        return None
    entries: list[DirtyEntry] = []
    for line in text.splitlines():
        if not line.startswith("snapshot "):
            continue
        fields = line.split(" ", 4)
        if len(fields) != 5:
            continue
        _, encoded_status, file_type, raw_fingerprint, encoded_path = fields
        try:
            status = base64.b64decode(encoded_status, validate=True).decode("utf-8")
            path = base64.b64decode(encoded_path, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        entries.append(
            DirtyEntry(
                path=path,
                status=status,
                file_type=file_type,
                fingerprint=None if raw_fingerprint == "-" else raw_fingerprint,
            )
        )
    return sorted(entries)


def serialize_snapshot(entries: list[DirtyEntry]) -> str:
    """Serialize a snapshot with a format version for later migrations."""
    return json.dumps(
        {"version": SNAPSHOT_VERSION, "entries": [asdict(entry) for entry in entries]},
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_snapshot(raw: Any) -> list[DirtyEntry] | None:
    """Read the versioned format. Legacy path arrays return None."""
    if not raw:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != SNAPSHOT_VERSION:
        return None
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        return None
    entries: list[DirtyEntry] = []
    try:
        for entry in raw_entries:
            if not isinstance(entry, dict):
                return None
            fingerprint = entry.get("fingerprint")
            entries.append(
                DirtyEntry(
                    path=str(entry["path"]),
                    status=str(entry["status"]),
                    file_type=str(entry["file_type"]),
                    fingerprint=str(fingerprint) if fingerprint is not None else None,
                )
            )
    except KeyError:
        return None
    return sorted(entries)


def snapshot_digest(entries: list[DirtyEntry]) -> str:
    """Return a stable audit fingerprint for one complete snapshot."""
    return "sha256:" + hashlib.sha256(serialize_snapshot(entries).encode()).hexdigest()


def legacy_paths(raw: Any) -> list[str] | None:
    """Read path-only task snapshots written before versioned snapshots."""
    if raw is None:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list):
        return None
    return sorted({str(path) for path in value})
