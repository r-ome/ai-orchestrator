from pathlib import Path

from app.controller.store import ControllerStore
from app.platform.remote import normalize_remote_url, project_id_for_remote


def test_remote_forms_share_one_normalized_project_identity() -> None:
    remotes = [
        "git@github.com:owner/repo.git",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
    ]

    assert {normalize_remote_url(remote) for remote in remotes} == {
        "https://github.com/owner/repo"
    }
    assert len({project_id_for_remote(remote) for remote in remotes}) == 1


def test_v1_registration_strips_userinfo_before_persistence(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    raw_remote = "https://token-value@GitHub.com/owner/repo.git"

    project = store.register_v1_project(
        project_id=project_id_for_remote(raw_remote),
        remote_url=raw_remote,
        default_branch="main",
        mirror_volume="project-mirror",
        created_at="2026-08-11T00:00:00Z",
    )

    assert project["remote_url"] == "https://github.com/owner/repo"
    assert "token-value" not in str(project)
    with store._connection() as connection:
        stored_remote = connection.execute(
            "SELECT remote_url FROM projects WHERE id = ?", (project["id"],)
        ).fetchone()[0]
        event_payloads = connection.execute(
            "SELECT payload_json FROM events"
        ).fetchall()
    assert "token-value" not in stored_remote
    assert all("token-value" not in str(payload[0]) for payload in event_payloads)
