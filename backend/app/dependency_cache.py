import base64
import hashlib
import io
import shlex
import tarfile
from typing import Any

import yaml
from docker.client import DockerClient
from docker.errors import NotFound

from app.platform.labels import LABEL_DATA_MANAGED, LABEL_PERSISTENT, LABEL_SANDBOX_ID
from app.previews._shared import _safe_relative_path, _slug
from app.previews.config import PreviewSettings
from app.previews.detection import ENVIRONMENT_FILE_NAMES, is_detection_file
from app.previews.errors import PreviewOperationError
from app.previews.models import PreviewConfiguration
from app.previews.resources import _decode_preview_archive, _run_preview_command

MAX_CONTEXT_BYTES = 512 * 1024 * 1024
# Inspection commands were previously unbounded.  The archive cap covers a
# 512 MiB Docker build context after base64 encoding, which stays text-safe.
PREVIEW_ARCHIVE_MAX_LOG_BYTES = 716_000_000
# Priority order for the lockfile that keys a sandbox's dependency volume.
_LOCKFILE_NAMES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
)

_DEPENDENCY_READY_MARKER = ".orchestrator-install-complete"

def _volume_runtime_files(
    docker_client: DockerClient,
    volume_name: str,
    settings: PreviewSettings,
) -> dict[str, bytes]:
    command = (
        "set -eu\n"
        "cd /workspace\n"
        "find . -maxdepth 5 -type f \\( -name 'compose.yaml' -o -name 'compose.yml' "
        "-o -name 'docker-compose.yaml' -o -name 'docker-compose.yml' "
        "-o -name 'Dockerfile*' -o -name '.dockerignore' "
        "-o -name 'package.json' -o -name 'package-lock.json' "
        "-o -name 'npm-shrinkwrap.json' -o -name 'pnpm-lock.yaml' "
        "-o -name 'yarn.lock' -o -name 'pyproject.toml' "
        "-o -name 'requirements*.txt' -o -name 'Pipfile' "
        "-o -name 'Pipfile.lock' -o -name 'poetry.lock' -o -name 'uv.lock' "
        "-o -name 'schema.prisma' "
        "-o -name 'vite.config.*' -o -name 'next.config.*' "
        "-o -path './.agent/preview.yaml' -o -name 'index.html' \\) "
        f"-size -{settings.maximum_file_bytes + 1}c -print > /tmp/files\n"
        # tar exits non-zero on an empty file list, so skip it when nothing matched.
        "if [ -s /tmp/files ]; then tar -cf - -T /tmp/files | base64 | tr -d '\\n'; fi\n"
    )
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
        max_log_bytes=PREVIEW_ARCHIVE_MAX_LOG_BYTES,
    )
    if not output:
        return {}
    archive_output = _decode_preview_archive(output)
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_output), mode="r:*") as archive:
            for member in archive:
                normalized = member.name.removeprefix("./")
                if not member.isfile() or not is_detection_file(normalized):
                    continue
                if member.size > settings.maximum_file_bytes:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                content = source.read(settings.maximum_file_bytes + 1)
                total += len(content)
                if total > settings.maximum_snapshot_bytes:
                    raise PreviewOperationError(
                        422,
                        "Protected runtime files exceed the inspection limit",
                    )
                files[normalized] = content
    except tarfile.TarError as error:
        raise PreviewOperationError(502, "Sandbox inspection returned invalid data") from error
    return files


def _lockfile_digest(files: dict[str, bytes]) -> str:
    """Digests the first root lockfile found, in `_LOCKFILE_NAMES` order.

    `files` comes from `_volume_runtime_files`, which already reads every
    name in `_LOCKFILE_NAMES` from the sandbox volume root, so no new
    volume-read path is needed here.
    """
    for name in _LOCKFILE_NAMES:
        content = files.get(name)
        if content is not None:
            return hashlib.sha256(content).hexdigest()
    return hashlib.sha256(b"none").hexdigest()


def _dependency_volume_ready(
    docker_client: DockerClient,
    settings: PreviewSettings,
    volume_name: str,
) -> bool:
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=[
            "sh",
            "-c",
            (
                f"if [ -f /workspace/{_DEPENDENCY_READY_MARKER} ]; "
                "then printf ready; fi"
            ),
        ],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
    )
    return bool(output.strip())


def _volume_environment_files(
    docker_client: DockerClient,
    volume_name: str,
    settings: PreviewSettings,
) -> dict[str, bytes]:
    """Reads the project volume's top-level env files. Never feeds hashes/baselines."""
    name_clauses = " -o ".join(
        f"-name {shlex.quote(name)}" for name in ENVIRONMENT_FILE_NAMES
    )
    command = (
        "set -eu\n"
        "cd /workspace\n"
        f"find . -maxdepth 1 -type f \\( {name_clauses} \\) "
        f"-size -{settings.maximum_file_bytes + 1}c -print > /tmp/files\n"
        # tar exits non-zero on an empty file list, so skip it when nothing matched.
        "if [ -s /tmp/files ]; then tar -cf - -T /tmp/files | base64 | tr -d '\\n'; fi\n"
    )
    output = _run_preview_command(
        docker_client,
        image=settings.inspection_image,
        command=["sh", "-c", command],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        tmpfs_size="32m",
        max_log_bytes=PREVIEW_ARCHIVE_MAX_LOG_BYTES,
    )
    if not output:
        return {}
    archive_output = _decode_preview_archive(output)
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_output), mode="r:*") as archive:
            for member in archive:
                normalized = member.name.removeprefix("./")
                if not member.isfile() or normalized not in ENVIRONMENT_FILE_NAMES:
                    continue
                if member.size > settings.maximum_file_bytes:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                content = source.read(settings.maximum_file_bytes + 1)
                total += len(content)
                if total > settings.maximum_snapshot_bytes:
                    raise PreviewOperationError(
                        422,
                        "Environment files exceed the inspection limit",
                    )
                files[normalized] = content
    except tarfile.TarError as error:
        raise PreviewOperationError(502, "Sandbox inspection returned invalid data") from error
    return files


def _volume_context_tar(
    docker_client: DockerClient,
    volume_name: str,
    context: str,
    inspection_image: str,
) -> bytes:
    relative = _safe_relative_path(context, field="build context", allow_dot=True)
    directory = "/workspace" if relative == "." else f"/workspace/{relative}"
    output = _run_preview_command(
        docker_client,
        image=inspection_image,
        command=["sh", "-c", f"tar -C {shlex.quote(directory)} -cf - . | base64 | tr -d '\\n'"],
        volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
        max_log_bytes=PREVIEW_ARCHIVE_MAX_LOG_BYTES,
    )
    output_bytes = _decode_preview_archive(output)
    if len(output_bytes) > MAX_CONTEXT_BYTES:
        raise PreviewOperationError(422, "Docker build context exceeds 512 MiB")
    return output_bytes


def _write_preview_manifest(
    docker_client: DockerClient,
    volume_name: str,
    inspection_image: str,
    config: PreviewConfiguration,
) -> None:
    document = yaml.safe_dump(
        config.model_dump(mode="json"),
        sort_keys=True,
        default_flow_style=False,
    ).encode()
    encoded = base64.b64encode(document).decode("ascii")
    _run_preview_command(
        docker_client,
        image=inspection_image,
        command=[
            "sh",
            "-c",
            (
                "set -eu; mkdir -p /workspace/.agent; "
                "printf '%s' \"$PREVIEW_MANIFEST\" | base64 -d "
                "> /workspace/.agent/preview.yaml"
            ),
        ],
        environment={"PREVIEW_MANIFEST": encoded},
        volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
        tmpfs_size="8m",
    )


def _dependency_volume_name(sandbox_id: str, lockfile_digest: str) -> str:
    return f"orchestrator-deps-{sandbox_id[:12]}-{lockfile_digest[:12]}"


def _dependency_volume(
    docker_client: DockerClient,
    sandbox_id: str,
    lockfile_digest: str,
    labels: dict[str, str],
) -> Any:
    """Gets or creates the dependency volume keyed by sandbox and lockfile digest.

    Labeled like a persistent `_data_volume` so cleanup never removes it: the
    volume is reused across runs and across rebuilds as long as the lockfile
    is unchanged, and a lockfile change earns a fresh volume automatically.
    """
    name = _dependency_volume_name(sandbox_id, lockfile_digest)
    try:
        volume = docker_client.volumes.get(name)
    except NotFound:
        volume = None
    if volume is not None:
        existing_labels = volume.attrs.get("Labels") or {}
        if (
            existing_labels.get(LABEL_DATA_MANAGED) != "true"
            or existing_labels.get(LABEL_PERSISTENT) != "true"
            or existing_labels.get(LABEL_SANDBOX_ID) != sandbox_id
        ):
            raise PreviewOperationError(
                409,
                f"Docker volume '{name}' is not trusted dependency data",
            )
        return volume
    return docker_client.volumes.create(
        name=name,
        driver="local",
        labels={
            **labels,
            LABEL_SANDBOX_ID: sandbox_id,
            LABEL_DATA_MANAGED: "true",
            LABEL_PERSISTENT: "true",
        },
    )


def _run_volume_name(run_id: str, logical_name: str) -> str:
    return f"orchestrator-preview-{run_id[:12]}-{_slug(logical_name)[:24]}"


def _data_volume(
    docker_client: DockerClient,
    run_id: str,
    logical_name: str,
    labels: dict[str, str],
    persistent: bool,
) -> Any:
    if persistent:
        sandbox_id = labels[LABEL_SANDBOX_ID]
        name = (
            f"orchestrator-preview-persistent-{sandbox_id[:12]}-"
            f"{_slug(logical_name)[:24]}"
        )
        try:
            volume = docker_client.volumes.get(name)
        except NotFound:
            volume = None
        if volume is not None:
            existing_labels = volume.attrs.get("Labels") or {}
            if (
                existing_labels.get(LABEL_DATA_MANAGED) != "true"
                or existing_labels.get(LABEL_PERSISTENT) != "true"
                or existing_labels.get(LABEL_SANDBOX_ID) != sandbox_id
            ):
                raise PreviewOperationError(
                    409,
                    f"Docker volume '{name}' is not trusted preview data",
                )
            return volume
    else:
        name = _run_volume_name(run_id, logical_name)
    return docker_client.volumes.create(
        name=name,
        driver="local",
        labels={
            **labels,
            LABEL_DATA_MANAGED: "true",
            LABEL_PERSISTENT: "true" if persistent else "false",
        },
    )
