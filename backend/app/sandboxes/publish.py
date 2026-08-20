"""Publication through remote Git and the controller-side GitHub API."""

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from docker.client import DockerClient

from app.containers.git import (
    GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE,
    GitWriteCredentialSource,
    assert_workspace_has_no_remotes,
    push_mirror_to_remote,
    push_workspace_to_mirror,
    remote_branch_sha,
)
from app.controller.store import ControllerStore
from app.previews.config import PreviewSettings
from app.sandboxes.feature_target import (
    FeatureTarget,
    FeatureTargetError,
    ensure_target_unchanged,
)

GITHUB_API_URL = "https://api.github.com"
_GITHUB_API_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class PublishOutcome:
    remote_branch: str
    last_pushed_commit: str
    remote_branch_sha: str
    pushed: bool


class PublishError(RuntimeError):
    """A publish refusal that maps directly to an HTTP response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class GitHubRepository:
    """The GitHub repository selected by a credential-free project remote."""

    owner: str
    name: str


@dataclass(frozen=True)
class PullRequest:
    """The verified subset of GitHub's pull request response."""

    number: int
    url: str
    state: str
    merged_at: str | None = None


class GitHubWriteCredentialSource(Protocol):
    """Provides the controller's write credential for the GitHub API."""

    def write_token(self) -> str | None:
        """Return a write token, or ``None`` when one is not configured."""


class EnvironmentGitHubWriteCredentialSource:
    """Read the GitHub API write token from the controller environment only."""

    def write_token(self) -> str | None:
        return os.environ.get(GITHUB_WRITE_TOKEN_ENVIRONMENT_VARIABLE)


class GitHubApiError(RuntimeError):
    """A deliberately token-free GitHub API failure."""


class GitHubPullRequestClient:
    """Perform PR discovery, creation, and verification on the controller."""

    def __init__(
        self,
        credential_source: GitHubWriteCredentialSource | None = None,
        *,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        self._credential_source = (
            credential_source or EnvironmentGitHubWriteCredentialSource()
        )
        self._api_url = api_url.rstrip("/")

    def find_by_head(
        self, repository: GitHubRepository, head_branch: str
    ) -> PullRequest | None:
        """Find an existing PR before attempting any create request."""
        payload = self._request(
            "GET",
            repository,
            "/pulls",
            params={
                "state": "all",
                "head": f"{repository.owner}:{head_branch}",
                "per_page": "100",
            },
            expected_statuses={200},
            operation="discovery",
        )
        if not isinstance(payload, list):
            raise GitHubApiError(
                "GitHub pull request discovery returned an invalid response"
            )
        candidates = [
            pull_request_from_payload(item)
            for item in payload
            if isinstance(item, dict)
        ]
        if not candidates:
            return None
        # GitHub can return historical closed PRs for the same branch. Prefer
        # an open PR, because it is the branch's live publication.
        return next(
            (
                candidate
                for candidate in candidates
                if candidate.state.lower() == "open"
            ),
            candidates[0],
        )

    def create(
        self,
        repository: GitHubRepository,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
    ) -> PullRequest:
        """Create a PR. The caller must discover first."""
        payload = self._request(
            "POST",
            repository,
            "/pulls",
            json_body={"head": head_branch, "base": base_branch, "title": title},
            expected_statuses={201},
            operation="creation",
        )
        return pull_request_from_payload(payload)

    def get(self, repository: GitHubRepository, number: int) -> PullRequest:
        """Re-query a PR so a create response is never treated as proof."""
        payload = self._request(
            "GET",
            repository,
            f"/pulls/{number}",
            expected_statuses={200},
            operation="verification",
        )
        return pull_request_from_payload(payload)

    def _request(
        self,
        method: str,
        repository: GitHubRepository,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
        expected_statuses: set[int],
        operation: str,
    ) -> Any:
        token = self._credential_source.write_token()
        if not token:
            raise GitHubApiError(
                "GitHub write credentials are not configured in the controller environment"
            )
        try:
            response = requests.request(
                method,
                f"{self._api_url}/repos/{repository.owner}/{repository.name}{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params=params,
                json=json_body,
                timeout=_GITHUB_API_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            # Do not include the exception text. A transport adapter can include
            # request details, including headers, in that text.
            raise GitHubApiError(
                f"GitHub pull request {operation} request failed"
            ) from error
        if response.status_code not in expected_statuses:
            # Never include response text or headers. GitHub's error body can
            # reflect input, and a mocked or proxy response could reflect auth.
            raise GitHubApiError(
                f"GitHub pull request {operation} failed (HTTP {response.status_code})"
            )
        try:
            return response.json()
        except ValueError as error:
            raise GitHubApiError(
                f"GitHub pull request {operation} returned invalid JSON"
            ) from error


def github_repository_from_remote(remote_url: str) -> GitHubRepository:
    """Resolve a normalized github.com remote to the GitHub API resource."""
    parsed = urlsplit(remote_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname != "github.com" or len(parts) != 2:
        raise PublishError(
            424, "GitHub pull request publishing requires a github.com remote"
        )
    return GitHubRepository(owner=parts[0], name=parts[1])


def pull_request_from_payload(payload: object) -> PullRequest:
    """Validate only the observed PR values that the controller persists."""
    if not isinstance(payload, dict):
        raise GitHubApiError("GitHub pull request response has an invalid shape")
    number = payload.get("number")
    url = payload.get("html_url")
    state = payload.get("state")
    merged_at = payload.get("merged_at")
    if (
        not isinstance(number, int)
        or number < 1
        or not isinstance(url, str)
        or not url
        or not isinstance(state, str)
        or not state
    ):
        raise GitHubApiError("GitHub pull request response is missing observed fields")
    return PullRequest(
        number=number,
        url=url,
        state=state,
        merged_at=merged_at if isinstance(merged_at, str) and merged_at else None,
    )


def discover_or_create_pull_request(
    *,
    remote_url: str,
    remote_branch: str,
    base_branch: str,
    title: str,
    client: GitHubPullRequestClient | None = None,
) -> PullRequest:
    """Discover by head branch, create only when absent, then verify it."""
    repository = github_repository_from_remote(remote_url)
    github = client or GitHubPullRequestClient()
    observed = github.find_by_head(repository, remote_branch)
    if observed is None:
        observed = github.create(
            repository,
            head_branch=remote_branch,
            base_branch=base_branch,
            title=title,
        )
    verified = github.get(repository, observed.number)
    if verified.number != observed.number:
        raise GitHubApiError(
            "GitHub pull request verification returned a different pull request"
        )
    return verified


def reviewed_target(
    store: ControllerStore,
    *,
    sandbox_id: str,
    feature_branch: str,
) -> FeatureTarget:
    """Require an approved, exact review target before any Git container runs."""
    review = store.latest_completed_delegation_review_for_sandbox(sandbox_id)
    if review is None:
        raise PublishError(409, "An approved feature review is required before publish")
    try:
        result = json.loads(str(review.get("result_json") or "{}"))
    except ValueError as error:
        raise PublishError(
            409, "The latest feature review has an invalid result"
        ) from error
    if not isinstance(result, dict) or result.get("approved") is not True:
        raise PublishError(409, "An approved feature review is required before publish")
    target = FeatureTarget(
        base_branch=str(review["base_branch"]),
        base_commit=str(review["base_commit"]),
        head_commit=str(review["head_commit"]),
    )
    if target.base_branch != feature_branch:
        raise PublishError(
            409,
            f"Reviewed branch '{target.base_branch}' is not sandbox feature branch '{feature_branch}'",
        )
    return target


def publish_reviewed_feature(
    docker_client: DockerClient,
    *,
    store: ControllerStore,
    preview_settings: PreviewSettings,
    sandbox_id: str,
    workspace_volume: str,
    mirror_volume: str,
    feature_branch: str,
    remote_branch: str,
    credential_source: GitWriteCredentialSource | None = None,
    # Phase 8 step 2 will supply the observed PR state to this extension point.
    # This endpoint never selects it, and plain --force has no code path.
    intentional_pre_pr_rebase: bool = False,
) -> PublishOutcome:
    """Push a reviewed branch through the mirror without giving it a remote."""
    target = reviewed_target(
        store, sandbox_id=sandbox_id, feature_branch=feature_branch
    )
    try:
        # This is the existing refuse-first review shape. It proves that the
        # branch and HEAD still match the reviewed commit before network Git.
        ensure_target_unchanged(
            docker_client,
            preview_settings,
            store,
            sandbox_id,
            target,
        )
    except FeatureTargetError as error:
        raise PublishError(error.status_code, error.detail) from error

    publication = store.sandbox_publication(sandbox_id)
    if intentional_pre_pr_rebase and _has_observed_pr(publication):
        raise PublishError(
            409,
            "--force-with-lease is forbidden after a pull request exists",
        )

    # The remote branch is the idempotency anchor. Query it before writing the
    # mirror so a retry of the same reviewed commit does not issue a push.
    observed_sha = remote_branch_sha(
        docker_client,
        image=preview_settings.git_image,
        mirror_volume=mirror_volume,
        remote_branch=remote_branch,
        credential_source=credential_source,
        ensure_image=True,
    )
    if observed_sha == target.head_commit:
        assert_workspace_has_no_remotes(
            docker_client,
            image=preview_settings.git_image,
            workspace_volume=workspace_volume,
            ensure_image=True,
        )
        return PublishOutcome(
            remote_branch=remote_branch,
            last_pushed_commit=target.head_commit,
            remote_branch_sha=observed_sha,
            pushed=False,
        )

    mirror_commit = push_workspace_to_mirror(
        docker_client,
        image=preview_settings.git_image,
        workspace_volume=workspace_volume,
        mirror_volume=mirror_volume,
        feature_branch=feature_branch,
        remote_branch=remote_branch,
        reviewed_head=target.head_commit,
        ensure_image=True,
    )
    if mirror_commit != target.head_commit:
        raise PublishError(409, "Mirror did not receive the reviewed feature commit")
    push_mirror_to_remote(
        docker_client,
        image=preview_settings.git_image,
        mirror_volume=mirror_volume,
        remote_branch=remote_branch,
        credential_source=credential_source,
        force_with_lease=intentional_pre_pr_rebase,
        ensure_image=True,
    )
    verified_sha = remote_branch_sha(
        docker_client,
        image=preview_settings.git_image,
        mirror_volume=mirror_volume,
        remote_branch=remote_branch,
        credential_source=credential_source,
        ensure_image=True,
    )
    if verified_sha != target.head_commit:
        raise PublishError(
            424, "Remote branch did not verify the reviewed feature commit"
        )
    assert_workspace_has_no_remotes(
        docker_client,
        image=preview_settings.git_image,
        workspace_volume=workspace_volume,
        ensure_image=True,
    )
    return PublishOutcome(
        remote_branch=remote_branch,
        last_pushed_commit=target.head_commit,
        remote_branch_sha=verified_sha,
        pushed=True,
    )


def _has_observed_pr(publication: dict[str, object] | None) -> bool:
    return publication is not None and publication.get("pr_number") is not None
