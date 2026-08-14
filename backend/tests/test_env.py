from pathlib import Path

import pytest

from app.env import load_env


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_assignments_reach_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLANNING_CLAUDE_MODEL", raising=False)
    path = _write(tmp_path, "PLANNING_CLAUDE_MODEL=claude-opus-5\n")

    applied = load_env(path)

    assert applied == {"PLANNING_CLAUDE_MODEL": "claude-opus-5"}


def test_a_real_environment_variable_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLANNING_CODEX_MODEL", "gpt-5.6-terra")
    path = _write(tmp_path, "PLANNING_CODEX_MODEL=gpt-5.6-sol\n")

    assert load_env(path) == {}


def test_comments_blanks_and_malformed_lines_are_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEPT", raising=False)
    path = _write(tmp_path, "# a comment\n\nno_equals_sign\n  KEPT = 'value'  \n")

    assert load_env(path) == {"KEPT": "value"}


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env(tmp_path / "absent") == {}
