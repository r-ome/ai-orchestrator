import shlex
from pathlib import PurePosixPath

from docker.client import DockerClient
from docker.types import Mount

from app.controller.config import get_controller_settings
from app.previews.config import PreviewSettings
from app.previews.errors import PreviewOperationError
from app.previews.resources import _run_preview_command


# The env files a preview must never read. Same pair the copy-time exclusion
# used, so a live preview loses nothing the copied workspace already hid.
_MASKED_ENVIRONMENT_NAMES = (".env", ".env.local")
# Docker bind-mounts a character device onto the env paths, but Vite treats the
# device-backed paths as changing files and restarts forever. Use one stable,
# empty regular file instead. Docker creates the target file when it is absent,
# and the regular source keeps its inode metadata stable for file watchers.
_MASK_SOURCE_NAME = "preview-env-mask"
# Refuse to start rather than leave one env file unmasked.
_MAXIMUM_ENVIRONMENT_MASKS = 100


def _environment_file_paths(
    docker_client: DockerClient,
    settings: PreviewSettings,
    volume_name: str,
) -> list[str]:
    """Lists every env file in the sandbox, relative to the volume root."""
    name_clauses = " -o ".join(
        f"-name {shlex.quote(name)}" for name in _MASKED_ENVIRONMENT_NAMES
    )
    command = (
        "set -eu\n"
        "cd /workspace\n"
        f"find . -type f \\( {name_clauses} \\) "
        "-not -path './.git/*' -not -path './node_modules/*' -print0\n"
    )
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
    )
    paths = []
    for entry in output.encode("utf-8", errors="replace").split(b"\0"):
        # -print0 keeps a newline in a filename from forging a second path.
        text = entry.decode("utf-8", errors="replace").removeprefix("./")
        if not text or ".." in PurePosixPath(text).parts:
            continue
        paths.append(text)
    return sorted(set(paths))


def _environment_masks(
    docker_client: DockerClient,
    settings: PreviewSettings,
    volume_name: str,
) -> list[Mount]:
    """Builds the mounts that make a preview's env files unreadable.

    A mount, not a copy-time exclusion: the container sees the mask for as long
    as it runs, so a coding agent writing `.env` after the preview started
    changes the sandbox and not what the preview reads. The two root paths are
    masked whether or not they exist yet, which is what closes that hole;
    deeper paths can only be masked where a file already sits, because Docker
    materialises a missing bind target inside the sandbox volume itself.
    """
    mask_source = _ensure_mask_source()
    paths = list(_MASKED_ENVIRONMENT_NAMES)
    for path in _environment_file_paths(docker_client, settings, volume_name):
        if path not in paths:
            paths.append(path)
    if len(paths) > _MAXIMUM_ENVIRONMENT_MASKS:
        raise PreviewOperationError(
            422,
            f"Sandbox holds more than {_MAXIMUM_ENVIRONMENT_MASKS} environment "
            "files; a preview cannot mask them all",
        )
    return [
        Mount(
            target=f"/workspace/{path}",
            source=mask_source,
            type="bind",
            read_only=True,
        )
        for path in paths
    ]


def _ensure_mask_source() -> str:
    """Returns a stable, empty regular file that Docker can bind over env files."""
    path = get_controller_settings().data_directory / _MASK_SOURCE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError("mask source is not a regular file")
        if not path.exists():
            path.touch(mode=0o600)
        elif path.stat().st_size:
            path.write_bytes(b"")
        path.chmod(0o600)
    except OSError as error:
        raise PreviewOperationError(
            503,
            "Preview environment masking is unavailable",
        ) from error
    return str(path)


def _exclude_preview_masks(
    docker_client: DockerClient,
    image: str,
    project_volume: str,
) -> None:
    """Keeps the root env masks out of `git status` in the sandbox.

    Docker creates an absent bind target as an empty file, so masking a `.env`
    that does not exist yet writes one into the sandbox volume. Untracked, it
    would fail every task completion report on the dirty-tree rule. The entries
    go in `.git/info/exclude`, which is local to the sandbox and not history.
    """
    marker = "# orchestrator preview masks"
    lines = "\\n".join((marker, *_MASKED_ENVIRONMENT_NAMES))
    script = (
        "set -eu\n"
        'exclude=/project/.git/info/exclude\n'
        '[ -d /project/.git ] || exit 0\n'
        'mkdir -p /project/.git/info\n'
        '[ -f "$exclude" ] || : > "$exclude"\n'
        f'if grep -qxF {shlex.quote(marker)} "$exclude"; then exit 0; fi\n'
        f'printf "{lines}\\n" >> "$exclude"\n'
    )
    _run_preview_command(
        docker_client,
        image=image,
        command=["sh", "-c", script],
        volumes={project_volume: {"bind": "/project", "mode": "rw"}},
        tmpfs_size="32m",
    )
