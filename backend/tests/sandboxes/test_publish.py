import pytest

from app.delegation.service import DelegationOperationError
from app.previews.config import PreviewSettings
from app.sandboxes import git
from app.sandboxes import publish


HEAD = "b" * 40


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _StaticGitHubWriteCredentialSource:
    def __init__(self, token: str) -> None:
        self.token = token

    def write_token(self) -> str:
        return self.token


class _Store:
    def __init__(self, review: dict[str, object] | None, publication: dict[str, object] | None = None) -> None:
        self.review = review
        self.publication = publication

    def latest_completed_delegation_review_for_sandbox(self, _sandbox_id: str) -> dict[str, object] | None:
        return self.review

    def sandbox_publication(self, _sandbox_id: str) -> dict[str, object] | None:
        return self.publication


def _settings() -> PreviewSettings:
    return PreviewSettings(
        inspection_image="alpine:latest",
        default_expiry_minutes=30,
        maximum_file_bytes=1,
        maximum_snapshot_bytes=1,
        proposal_lifetime_seconds=1,
        prepare_timeout_seconds=1,
        build_timeout_seconds=1,
        git_image="alpine/git:latest",
    )


def _approved_review() -> dict[str, object]:
    return {
        "base_branch": "feature/publish-test",
        "base_commit": "a" * 40,
        "head_commit": HEAD,
        "result_json": '{"approved": true}',
    }


def _publish(store: _Store) -> publish.PublishOutcome:
    return publish.publish_reviewed_feature(
        object(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        preview_settings=_settings(),
        sandbox_id="sandbox",
        workspace_volume="workspace",
        mirror_volume="mirror",
        feature_branch="feature/publish-test",
        remote_branch="feature/publish-test",
    )


@pytest.mark.parametrize(
    ("merged_at", "expected"),
    [
        ("2026-08-14T00:00:00Z", "2026-08-14T00:00:00Z"),
        (None, None),
        ("", None),
        ({"unexpected": "value"}, None),
    ],
)
def test_pull_request_payload_reads_only_a_nonempty_merged_timestamp(
    merged_at: object, expected: str | None
) -> None:
    pull_request = publish.pull_request_from_payload(
        {
            "number": 42,
            "html_url": "https://github.com/owner/repository/pull/42",
            "state": "closed",
            "merged_at": merged_at,
        }
    )

    assert pull_request.merged_at == expected


def test_publish_refuses_an_unreviewed_head_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        publish,
        "remote_branch_sha",
        lambda *_args, **_kwargs: pytest.fail("publish queried the network before review"),
    )

    with pytest.raises(publish.PublishError, match="approved feature review"):
        _publish(_Store(None))


def test_publish_refuses_a_moved_head_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        publish,
        "ensure_target_unchanged",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DelegationOperationError(409, "Sandbox HEAD changed after review")
        ),
    )
    monkeypatch.setattr(
        publish,
        "remote_branch_sha",
        lambda *_args, **_kwargs: pytest.fail("publish queried the network after a moved HEAD"),
    )

    with pytest.raises(publish.PublishError, match="Sandbox HEAD changed after review"):
        _publish(_Store(_approved_review()))


def test_publish_retry_converges_when_the_remote_already_has_the_reviewed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(publish, "ensure_target_unchanged", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish, "remote_branch_sha", lambda *_args, **_kwargs: HEAD)
    monkeypatch.setattr(
        publish,
        "push_workspace_to_mirror",
        lambda *_args, **_kwargs: calls.append("workspace-to-mirror"),
    )
    monkeypatch.setattr(
        publish,
        "push_mirror_to_remote",
        lambda *_args, **_kwargs: calls.append("mirror-to-remote"),
    )
    monkeypatch.setattr(
        publish,
        "assert_workspace_has_no_remotes",
        lambda *_args, **_kwargs: calls.append("remote-free"),
    )

    outcome = _publish(_Store(_approved_review()))

    assert outcome.pushed is False
    assert outcome.remote_branch_sha == HEAD
    assert calls == ["remote-free"]


def test_force_with_lease_extension_refuses_after_an_observed_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publish, "ensure_target_unchanged", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        publish,
        "remote_branch_sha",
        lambda *_args, **_kwargs: pytest.fail("force guard reached network"),
    )

    with pytest.raises(publish.PublishError, match="forbidden after a pull request"):
        publish.publish_reviewed_feature(
            object(),  # type: ignore[arg-type]
            store=_Store(_approved_review(), {"pr_number": 42}),  # type: ignore[arg-type]
            preview_settings=_settings(),
            sandbox_id="sandbox",
            workspace_volume="workspace",
            mirror_volume="mirror",
            feature_branch="feature/publish-test",
            remote_branch="feature/publish-test",
            intentional_pre_pr_rebase=True,
        )


def test_push_scripts_never_offer_plain_force() -> None:
    assert "--force " not in git._PUSH_MIRROR_TO_REMOTE_SCRIPT
    assert "--force-with-lease" in git._PUSH_MIRROR_TO_REMOTE_FORCE_WITH_LEASE_SCRIPT


def test_pr_discovery_creates_once_then_requeries_the_new_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    responses = iter(
        [
            _Response(200, []),
            _Response(201, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}),
            _Response(200, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}),
        ]
    )

    def request(*_args: object, **kwargs: object) -> _Response:
        calls.append(dict(kwargs))
        return next(responses)

    monkeypatch.setattr(publish.requests, "request", request)
    pull_request = publish.discover_or_create_pull_request(
        remote_url="https://github.com/owner/repo",
        remote_branch="feature/publish-test",
        base_branch="main",
        title="Publish test",
        client=publish.GitHubPullRequestClient(_StaticGitHubWriteCredentialSource("write-token")),
    )

    assert pull_request.number == 42
    assert [call["json"] for call in calls] == [None, {"head": "feature/publish-test", "base": "main", "title": "Publish test"}, None]
    assert calls[0]["params"] == {"state": "all", "head": "owner:feature/publish-test", "per_page": "100"}
    assert calls[1]["headers"] == calls[0]["headers"]


def test_pr_discovery_reuses_an_existing_head_branch_without_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = iter(
        [
            _Response(200, [{"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}]),
            _Response(200, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}),
        ]
    )

    def request(method: str, *_args: object, **_kwargs: object) -> _Response:
        calls.append(method)
        return next(responses)

    monkeypatch.setattr(publish.requests, "request", request)
    pull_request = publish.discover_or_create_pull_request(
        remote_url="https://github.com/owner/repo",
        remote_branch="feature/publish-test",
        base_branch="main",
        title="Publish test",
        client=publish.GitHubPullRequestClient(_StaticGitHubWriteCredentialSource("write-token")),
    )

    assert pull_request.number == 42
    assert calls == ["GET", "GET"]


def test_pr_creation_failure_retries_discovery_then_creates_exactly_one_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = iter(
        [
            _Response(200, []),
            _Response(500, {"message": "temporary failure"}),
            _Response(200, []),
            _Response(201, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}),
            _Response(200, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42", "state": "open"}),
        ]
    )

    def request(method: str, *_args: object, **_kwargs: object) -> _Response:
        calls.append(method)
        return next(responses)

    monkeypatch.setattr(publish.requests, "request", request)
    client = publish.GitHubPullRequestClient(_StaticGitHubWriteCredentialSource("write-token"))
    request_kwargs = {
        "remote_url": "https://github.com/owner/repo",
        "remote_branch": "feature/publish-test",
        "base_branch": "main",
        "title": "Publish test",
        "client": client,
    }

    with pytest.raises(publish.GitHubApiError, match="HTTP 500"):
        publish.discover_or_create_pull_request(**request_kwargs)
    pull_request = publish.discover_or_create_pull_request(**request_kwargs)

    assert pull_request.number == 42
    assert calls == ["GET", "POST", "GET", "POST", "GET"]


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_github_api_errors_never_include_the_write_token(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    token = "github-write-token-that-must-not-leak"
    monkeypatch.setattr(
        publish.requests,
        "request",
        lambda *_args, **_kwargs: _Response(status_code, {"message": token}),
    )

    with pytest.raises(publish.GitHubApiError) as raised:
        publish.discover_or_create_pull_request(
            remote_url="https://github.com/owner/repo",
            remote_branch="feature/publish-test",
            base_branch="main",
            title="Publish test",
            client=publish.GitHubPullRequestClient(_StaticGitHubWriteCredentialSource(token)),
        )

    assert token not in str(raised.value)
