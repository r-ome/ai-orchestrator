from docker.errors import ContainerError

from app.sandboxes.git import describe_git_failure


def _container_error(
    *, exit_status: int = 128, stderr: bytes | str | None
) -> ContainerError:
    return ContainerError(
        container="git-push",
        exit_status=exit_status,
        command=(
            "if [ ! -f /run/secrets/github_write_token ]; then\n"
            "  exit 70\n"
            "fi\n"
            "set -eu\n"
            "git -C /mirror push origin"
        ),
        image="alpine/git:latest",
        stderr=stderr,
    )


def test_describe_git_failure_summarizes_github_write_permission_denial() -> None:
    error = _container_error(
        stderr=(
            b"remote: Permission to r-ome/personal-blog.git denied to r-ome.\n"
            b"fatal: unable to access 'https://github.com/r-ome/personal-blog/': "
            b"The requested URL returned error: 403\n"
        )
    )

    message = describe_git_failure(error)

    assert "r-ome/personal-blog" in message
    assert "repo" in message
    assert "/run/secrets/github_write_token" not in message
    assert "set -eu" not in message
    assert "alpine/git" not in message


def test_describe_git_failure_returns_the_last_three_stderr_lines() -> None:
    error = _container_error(
        stderr="first\nsecond\nremote: third\nfourth\nfifth\n",
    )

    assert describe_git_failure(error) == "third; fourth; fifth"


def test_describe_git_failure_uses_exit_status_when_stderr_is_empty() -> None:
    error = _container_error(exit_status=70, stderr=b"")

    assert describe_git_failure(error) == "Git failed with exit status 70"


def test_describe_git_failure_returns_plain_exception_text() -> None:
    assert describe_git_failure(RuntimeError("boom")) == "boom"
