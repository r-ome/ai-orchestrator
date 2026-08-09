import hashlib
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from docker.client import DockerClient
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.volumes import Volume

from app.controller.store import ControllerStore
from app.projects.config import DEFAULT_COPY_IMAGE, ProjectSettings
from app.projects.models import (
    BrowseEntry,
    BrowseResponse,
    CopyProjectRequest,
    ProjectCopyJobsResponse,
    ProjectCopyJobStatus,
    ProjectRegistration,
    ProjectRegistrationsResponse,
)

LABEL_MANAGED = "orchestrator.project.managed"
LABEL_NAME = "orchestrator.project.name"
LABEL_SOURCE = "orchestrator.project.source"
LABEL_CREATED_AT = "orchestrator.project.created-at"
LABEL_COPY_MODE = "orchestrator.project.copy-mode"
LABEL_FILE_COUNT = "orchestrator.project.file-count"
LABEL_COPIED_BYTES = "orchestrator.project.copied-bytes"
LABEL_EXCLUDED_DIRECTORIES = "orchestrator.project.excluded-directories"
LABEL_COPY_IMAGE = "orchestrator.project.copy-image"
LABEL_STATUS_STORAGE = "orchestrator.project.copy-status-storage"
LABEL_COPY_JOB = "orchestrator.project.copy-job"
LABEL_COPY_JOB_ID = "orchestrator.project.copy-job-id"
LABEL_PROJECT_ID = "orchestrator.project.id"
LABEL_SANDBOX_ID = "orchestrator.sandbox.id"
LABEL_METADATA_MANAGED = "orchestrator.project.copy-metadata-managed"
LABEL_METADATA_VOLUME = "orchestrator.project.copy-metadata-volume"
COPY_CONTAINER_PREFIX = "orchestrator-project-copy-"
COPY_READER_LABEL = "orchestrator.project.copy-reader"
# Superseded storage kind: metadata lived under `.orchestrator` inside the
# agent-writable project volume, so an agent could rewrite its own copy
# status. Jobs created before the controller-volume move still carry this
# label and are read from their legacy location for continuity.
STATUS_STORAGE_PROJECT_VOLUME = "project-volume-v1"
# Current storage kind: metadata lives in a dedicated volume that is never
# mounted into an agent or preview container, so only the controller ever
# writes it.
STATUS_STORAGE_CONTROLLER_VOLUME = "controller-volume-v1"
LEGACY_COPY_METADATA_DIRECTORY = "/project/.orchestrator/copy-job"
CONTROLLER_METADATA_MOUNT = "/controller"
COPY_METADATA_DIRECTORY = CONTROLLER_METADATA_MOUNT
MAX_PROJECT_NAME_LENGTH = 100
SANDBOX_NUMBER_PATTERN = re.compile(r"-sandbox-(\d+)$", re.IGNORECASE)
_sandbox_creation_lock = Lock()
EXCLUDED_DIRECTORY_NAMES = (
    ".next",
    ".nox",
    ".nuxt",
    ".parcel-cache",
    ".pnpm-store",
    ".pytest_cache",
    ".ruff_cache",
    ".svelte-kit",
    ".tox",
    ".venv",
    ".vite",
    ".orchestrator",
    "__pycache__",
    "bower_components",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
    "venv",
)
EXCLUDED_DIRECTORY_NAME_SET = frozenset(EXCLUDED_DIRECTORY_NAMES)
COPY_EXCLUDE_ARGUMENTS = " ".join(
    argument
    for directory_name in EXCLUDED_DIRECTORY_NAMES
    for argument in (
        f"--exclude='./{directory_name}'",
        f"--exclude='*/{directory_name}'",
    )
)
COPY_COMMAND = [
    "sh",
    "-c",
    (
        "set -eu\n"
        "set -o pipefail\n"
        f"metadata_dir={COPY_METADATA_DIRECTORY}\n"
        "mkdir -p \"$metadata_dir\"\n"
        "log_file=\"$metadata_dir/copy.log\"\n"
        ": > \"$log_file\"\n"
        "started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')\n"
        "printf '%s' \"$started_at\" > \"$metadata_dir/started_at\"\n"
        "printf 'copying' > \"$metadata_dir/status\"\n"
        "printf 'copy started\\n' | tee -a \"$log_file\"\n"
        "set +e\n"
        f"tar -C /source {COPY_EXCLUDE_ARGUMENTS} -cf - . 2>&1 "
        "| tar -C /project -xf - 2>&1 | tee -a \"$log_file\"\n"
        "copy_exit=$?\n"
        "set -e\n"
        "if [ \"$copy_exit\" -eq 0 ]; then\n"
        "  printf 'completed' > \"$metadata_dir/status\"\n"
        "  : > \"$metadata_dir/error\"\n"
        "  printf 'copy completed\\n' | tee -a \"$log_file\"\n"
        "else\n"
        "  printf 'failed' > \"$metadata_dir/status\"\n"
        "  printf 'Copy command exited with code %s' \"$copy_exit\" "
        "> \"$metadata_dir/error\"\n"
        "  printf 'copy failed with exit code %s\\n' \"$copy_exit\" "
        "| tee -a \"$log_file\"\n"
        "fi\n"
        "printf '%s' \"$copy_exit\" > \"$metadata_dir/exit_code\"\n"
        "finished_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')\n"
        "printf '%s' \"$finished_at\" > \"$metadata_dir/finished_at\"\n"
        "exit \"$copy_exit\"\n"
    ),
]
#: Directories the controller and its agents create inside a sandbox. They are
#: not the project's files and must never reach a task branch or a review diff.
SANDBOX_SCAFFOLDING = (".agent", ".claude", ".orchestrator")

GIT_BASELINE_SCRIPT = (
    "set -eu\n"
    "cd /project\n"
    "if [ ! -d .git ]; then\n"
    "  git init -q -b main\n"
    "fi\n"
    'git config user.name "orchestrator"\n'
    'git config user.email "orchestrator@localhost"\n'
    # `.git/info/exclude` rather than `.gitignore`: the scaffolding belongs to
    # the sandbox, not to the project. An exclude entry is local to this clone,
    # so it never appears in the imported repository, in a commit, or in a diff
    # a person reviews — and it applies to every project without editing any.
    # Appended before the baseline commit below, so `git add -A` on a
    # repository with no history does not pick the directories up either.
    "mkdir -p .git/info\n"
    "touch .git/info/exclude\n"
    f'for scaffold in {" ".join(SANDBOX_SCAFFOLDING)}; do\n'
    '  if ! grep -qxF "/$scaffold/" .git/info/exclude; then\n'
    '    printf "/%s/\\n" "$scaffold" >> .git/info/exclude\n'
    "  fi\n"
    "done\n"
    "if ! git rev-parse HEAD >/dev/null 2>&1; then\n"
    "  git add -A\n"
    '  git commit -q -m "sandbox baseline" --allow-empty\n'
    "fi\n"
    "git rev-parse HEAD\n"
)


class ProjectOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def register_project(
    docker_client: DockerClient,
    settings: ProjectSettings,
    request: CopyProjectRequest,
) -> ProjectCopyJobStatus:
    source_path = _validated_source_path(request.path, settings.projects_root)
    file_count, copied_bytes = _project_inventory(source_path)
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    job_id = uuid4().hex
    project_id = uuid5(NAMESPACE_URL, f"orchestrator-project:{source_path}").hex
    sandbox_id = uuid4().hex

    volume: Volume | None = None
    metadata_volume: Volume | None = None
    helper: Container | None = None
    try:
        with _sandbox_creation_lock:
            registered_volumes = _registered_volumes(docker_client)
            project_name = _next_sandbox_name(registered_volumes, source_path)
            volume_name = _volume_name(project_name, source_path)
            metadata_volume_name = _metadata_volume_name(volume_name)
            try:
                docker_client.volumes.get(volume_name)
            except NotFound:
                pass
            else:
                raise ProjectOperationError(
                    409,
                    f"Docker volume '{volume_name}' already exists",
                )

            shared_labels = {
                LABEL_NAME: project_name,
                LABEL_SOURCE: str(source_path),
                LABEL_CREATED_AT: created_at,
                LABEL_COPY_MODE: "snapshot",
                LABEL_FILE_COUNT: str(file_count),
                LABEL_COPIED_BYTES: str(copied_bytes),
                LABEL_EXCLUDED_DIRECTORIES: ",".join(EXCLUDED_DIRECTORY_NAMES),
                LABEL_COPY_IMAGE: settings.copy_image,
                LABEL_STATUS_STORAGE: STATUS_STORAGE_CONTROLLER_VOLUME,
                LABEL_COPY_JOB_ID: job_id,
                LABEL_PROJECT_ID: project_id,
                LABEL_SANDBOX_ID: sandbox_id,
                LABEL_METADATA_VOLUME: metadata_volume_name,
            }
            volume = docker_client.volumes.create(
                name=volume_name,
                driver="local",
                labels={LABEL_MANAGED: "true", **shared_labels},
            )
            # Controller-owned: mounted read-write only into the copy job
            # below and read-only into status readers. Never mounted into an
            # agent or preview container, so an agent cannot rewrite its own
            # copy status.
            metadata_volume = docker_client.volumes.create(
                name=metadata_volume_name,
                driver="local",
                labels={LABEL_METADATA_MANAGED: "true", **shared_labels},
            )

        helper = docker_client.containers.create(
            image=settings.copy_image,
            command=COPY_COMMAND,
            name=f"{COPY_CONTAINER_PREFIX}{job_id}",
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            auto_remove=True,
            labels={LABEL_COPY_JOB: "true", **shared_labels},
            volumes={
                str(source_path): {"bind": "/source", "mode": "ro"},
                volume_name: {"bind": "/project", "mode": "rw"},
                metadata_volume_name: {"bind": CONTROLLER_METADATA_MOUNT, "mode": "rw"},
            },
        )
        helper.start()
    except ImageNotFound as error:
        _rollback_registration(volume, metadata_volume, helper)
        raise ProjectOperationError(
            424,
            f"Project copy image '{settings.copy_image}' is not available",
        ) from error
    except DockerException:
        _rollback_registration(volume, metadata_volume, helper)
        raise

    return _copy_job_from_container(helper, include_logs=False)


def list_registered_projects(
    docker_client: DockerClient,
) -> ProjectRegistrationsResponse:
    projects = [
        _project_from_volume(docker_client, volume)
        for volume in _registered_volumes(docker_client)
    ]
    projects.sort(key=lambda project: project.name.casefold())
    return ProjectRegistrationsResponse(count=len(projects), projects=projects)


def inspect_registered_project(
    docker_client: DockerClient,
    project_name: str,
) -> ProjectRegistration:
    for volume in _registered_volumes(docker_client):
        labels = volume.attrs.get("Labels") or {}
        if labels.get(LABEL_NAME, "").casefold() == project_name.casefold():
            return _project_from_volume(docker_client, volume)
    raise ProjectOperationError(404, f"Project '{project_name}' is not registered")


def ensure_sandbox_registered(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    *,
    project: ProjectRegistration | None = None,
) -> tuple[str, str, ProjectRegistration]:
    """Returns (sandbox_id, project_id, project), registering the sandbox row."""
    project = project or inspect_registered_project(docker_client, project_name)
    if not project.ready:
        raise ProjectOperationError(409, f"Project '{project_name}' is not ready")

    sandbox_id = getattr(project, "sandbox_id", "") or hashlib.sha256(
        f"sandbox:{project.volume_name}".encode()
    ).hexdigest()[:32]
    source_path = getattr(project, "source_path", "") or f"legacy:{project.name}"
    created_at = getattr(project, "created_at", "")
    project_key = project_id(source_path)
    controller_store.register_sandbox(
        sandbox_id=sandbox_id,
        project_id=project_key,
        project_name=project.name,
        source_path=source_path,
        volume_name=project.volume_name,
        status="ready",
        created_at=created_at,
    )
    return sandbox_id, project_key, project


def remove_project(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> dict[str, int | str]:
    """Stops and removes every Docker resource owned by one sandbox."""
    project = inspect_registered_project(docker_client, project_name)
    sandbox_id = project.sandbox_id
    project_key = project_id(project.source_path)
    siblings = [
        row
        for row in controller_store.sandboxes()
        if str(row.get("project_id")) == project_key
        and str(row.get("id")) != sandbox_id
    ]

    # Release a shared database schema before deleting its controller record.
    # The shared server itself is removed below when this is the last sandbox.
    try:
        from app.previews.service import _release_shared_database

        _release_shared_database(
            docker_client,
            controller_store,
            sandbox_id=sandbox_id,
        )
    except DockerException:
        pass

    volumes = docker_client.volumes.list(
        filters={"label": f"{LABEL_SANDBOX_ID}={sandbox_id}"},
    )
    try:
        project_volume = docker_client.volumes.get(project.volume_name)
    except NotFound:
        project_volume = None
    if project_volume is not None and all(
        volume.name != project_volume.name for volume in volumes
    ):
        volumes.append(project_volume)
    volume_names = {project.volume_name, *(volume.name for volume in volumes)}
    if not siblings:
        volumes.extend(
            docker_client.volumes.list(
                filters={"label": f"{LABEL_PROJECT_ID}={project_key}"},
            )
        )
        volume_names.update(volume.name for volume in volumes)

    containers = docker_client.containers.list(all=True)
    selected_containers = []
    for container in containers:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        mounts = container.attrs.get("Mounts") or []
        mounted_volumes = {
            str(mount.get("Name"))
            for mount in mounts
            if mount.get("Type") == "volume" and mount.get("Name")
        }
        owned = labels.get(LABEL_SANDBOX_ID) == sandbox_id
        if not siblings:
            owned = owned or labels.get(LABEL_PROJECT_ID) == project_key
        if owned or mounted_volumes & volume_names:
            selected_containers.append(container)

    removed_containers = 0
    for container in selected_containers:
        try:
            container.remove(force=True)
            removed_containers += 1
        except NotFound:
            continue

    networks = docker_client.networks.list(
        filters={"label": f"{LABEL_SANDBOX_ID}={sandbox_id}"},
    )
    if not siblings:
        networks.extend(
            docker_client.networks.list(
                filters={"label": f"{LABEL_PROJECT_ID}={project_key}"},
            )
        )
    removed_networks = 0
    seen_networks: set[str] = set()
    for network in networks:
        if network.id in seen_networks:
            continue
        seen_networks.add(network.id)
        try:
            network.remove()
            removed_networks += 1
        except NotFound:
            continue

    removed_volumes = 0
    seen_volumes: set[str] = set()
    for volume in volumes:
        if volume.name in seen_volumes:
            continue
        seen_volumes.add(volume.name)
        try:
            volume.remove(force=True)
            removed_volumes += 1
        except NotFound:
            continue

    controller_store.delete_sandbox(sandbox_id)
    return {
        "project_name": project.name,
        "removed_containers": removed_containers,
        "removed_networks": removed_networks,
        "removed_volumes": removed_volumes,
    }


def browse_project_folders(
    settings: ProjectSettings,
    path: str | None,
) -> BrowseResponse:
    try:
        root = settings.projects_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectOperationError(
            500,
            "Configured project root is unavailable",
        ) from error

    submitted_path = Path(path).expanduser() if path else root
    if not submitted_path.is_absolute():
        raise ProjectOperationError(400, "Browse path must be absolute")

    try:
        current = submitted_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ProjectOperationError(404, "Browse folder does not exist") from error
    except (OSError, RuntimeError) as error:
        raise ProjectOperationError(400, "Browse folder cannot be resolved") from error

    if not current.is_relative_to(root):
        raise ProjectOperationError(
            400,
            f"Browse folder must be inside '{root}'",
        )
    if not current.is_dir():
        raise ProjectOperationError(400, "Browse path must identify a directory")

    entries: list[BrowseEntry] = []
    try:
        with os.scandir(current) as directory_entries:
            for entry in directory_entries:
                try:
                    resolved_entry = Path(entry.path).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if not resolved_entry.is_dir() or not resolved_entry.is_relative_to(root):
                    continue
                entries.append(
                    BrowseEntry(
                        name=entry.name,
                        path=str(resolved_entry),
                        has_children=_has_browsable_child(resolved_entry, root),
                    )
                )
    except OSError as error:
        raise ProjectOperationError(403, "Browse folder cannot be read") from error

    entries.sort(key=lambda entry: entry.name.casefold())
    return BrowseResponse(
        root=str(root),
        path=str(current),
        parent=None if current == root else str(current.parent),
        entries=entries,
    )


def list_project_copy_jobs(
    docker_client: DockerClient,
) -> ProjectCopyJobsResponse:
    jobs = [
        _copy_job_from_volume(docker_client, volume, include_logs=False)
        for volume in _registered_volumes(docker_client)
    ]
    jobs.sort(key=lambda job: job.created_at, reverse=True)
    return ProjectCopyJobsResponse(count=len(jobs), jobs=jobs)


def inspect_project_copy_job(
    docker_client: DockerClient,
    job_id: str,
) -> ProjectCopyJobStatus:
    for volume in _registered_volumes(docker_client):
        labels = volume.attrs.get("Labels") or {}
        if labels.get(LABEL_COPY_JOB_ID) == job_id:
            return _copy_job_from_volume(
                docker_client,
                volume,
                include_logs=True,
            )
    raise ProjectOperationError(404, f"Copy job '{job_id}' was not found")


def _validated_source_path(path_value: str, projects_root: Path) -> Path:
    submitted_path = Path(path_value).expanduser()
    if not submitted_path.is_absolute():
        raise ProjectOperationError(400, "Project path must be absolute")

    try:
        resolved_root = projects_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectOperationError(
            500,
            "Configured project root is unavailable",
        ) from error

    try:
        source_path = submitted_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ProjectOperationError(404, "Project folder does not exist") from error
    except (OSError, RuntimeError) as error:
        raise ProjectOperationError(400, "Project folder cannot be resolved") from error

    if source_path == resolved_root or not source_path.is_relative_to(resolved_root):
        raise ProjectOperationError(
            400,
            f"Project folder must be inside '{resolved_root}'",
        )
    if not source_path.is_dir():
        raise ProjectOperationError(400, "Project path must identify a directory")
    return source_path


def _registered_volumes(docker_client: DockerClient) -> list[Volume]:
    return docker_client.volumes.list(
        filters={"label": f"{LABEL_MANAGED}=true"},
    )


def _next_sandbox_name(volumes: list[Volume], source_path: Path) -> str:
    source = str(source_path)
    existing_names = {
        (volume.attrs.get("Labels") or {}).get(LABEL_NAME, "").casefold()
        for volume in volumes
    }
    existing_numbers = []
    for volume in volumes:
        labels = volume.attrs.get("Labels") or {}
        if labels.get(LABEL_SOURCE) != source:
            continue
        match = SANDBOX_NUMBER_PATTERN.search(labels.get(LABEL_NAME, ""))
        if match:
            existing_numbers.append(int(match.group(1)))

    sandbox_number = max(existing_numbers, default=0) + 1
    base_name = _sandbox_base_name(source_path)
    while True:
        suffix = f"-sandbox-{sandbox_number}"
        truncated_base = base_name[: MAX_PROJECT_NAME_LENGTH - len(suffix)].rstrip(
            " ._-"
        )
        candidate = f"{truncated_base or 'project'}{suffix}"
        if candidate.casefold() not in existing_names:
            return candidate
        sandbox_number += 1


def _sandbox_base_name(source_path: Path) -> str:
    base_name = re.sub(r"[^A-Za-z0-9 ._-]+", "-", source_path.name)
    return base_name.strip(" ._-") or "project"


def _volume_name(project_name: str, source_path: Path) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", project_name.casefold()).strip("-._")
    slug = slug[:40] or "project"
    identity = f"{source_path}\0{project_name}".encode()
    identity_digest = hashlib.sha256(identity).hexdigest()[:10]
    return f"orchestrator-project-{slug}-{identity_digest}"


def _metadata_volume_name(volume_name: str) -> str:
    """Deterministic controller-owned sibling of a project volume.

    Named from the project volume so it never collides with another job's
    metadata and needs no separate uniqueness check.
    """
    return f"{volume_name}-controller-metadata"


def _project_inventory(source_path: Path) -> tuple[int, int]:
    file_count = 0
    copied_bytes = 0
    pending = [source_path]

    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if entry.name in EXCLUDED_DIRECTORY_NAME_SET:
                            continue
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        file_count += 1
                        copied_bytes += entry_stat.st_size
    except OSError as error:
        raise ProjectOperationError(
            422,
            "Project folder could not be inspected",
        ) from error

    return file_count, copied_bytes


def _project_from_volume(
    docker_client: DockerClient,
    volume: Volume,
) -> ProjectRegistration:
    attrs = volume.attrs
    labels = attrs.get("Labels") or {}
    copied_bytes = _integer(labels.get(LABEL_COPIED_BYTES))
    job_id = labels.get(LABEL_COPY_JOB_ID, "")
    job = _copy_job_from_volume(docker_client, volume, include_logs=False)
    copy_status = job.status
    return ProjectRegistration(
        sandbox_id=_sandbox_id(labels, volume.name),
        name=labels.get(LABEL_NAME, ""),
        source_path=labels.get(LABEL_SOURCE, ""),
        volume_name=volume.name,
        created_at=labels.get(LABEL_CREATED_AT, ""),
        copy_mode=labels.get(LABEL_COPY_MODE, "snapshot"),
        file_count=_integer(labels.get(LABEL_FILE_COUNT)),
        copied_bytes=copied_bytes,
        copied_size=_format_bytes(copied_bytes),
        driver=attrs.get("Driver", ""),
        mountpoint=attrs.get("Mountpoint", ""),
        copy_job_id=job_id,
        copy_status=copy_status,
        ready=copy_status == "completed",
        excluded_directories=_excluded_directories(labels),
    )


def _copy_job_from_volume(
    docker_client: DockerClient,
    volume: Volume,
    *,
    include_logs: bool,
) -> ProjectCopyJobStatus:
    labels = volume.attrs.get("Labels") or {}
    job_id = labels.get(LABEL_COPY_JOB_ID, "")
    try:
        container = docker_client.containers.get(f"{COPY_CONTAINER_PREFIX}{job_id}")
    except NotFound:
        persisted = _read_persisted_copy_status(
            docker_client,
            volume,
            labels,
            include_logs=include_logs,
        )
        return _copy_job_from_persisted(volume, labels, persisted)
    return _copy_job_from_container(container, include_logs=include_logs)


def _read_persisted_copy_status(
    docker_client: DockerClient,
    volume: Volume,
    labels: dict[str, str],
    *,
    include_logs: bool,
) -> dict[str, str]:
    storage = labels.get(LABEL_STATUS_STORAGE)
    if storage == STATUS_STORAGE_CONTROLLER_VOLUME:
        metadata_volume_name = labels.get(LABEL_METADATA_VOLUME, "")
        if not metadata_volume_name:
            # A job registered under this storage kind always carries the
            # metadata volume label; a missing one means the volume was
            # removed out of band. Report unknown rather than crash.
            return {}
        source_volume_name = metadata_volume_name
        source_mount = CONTROLLER_METADATA_MOUNT
        metadata_directory = CONTROLLER_METADATA_MOUNT
    elif storage == STATUS_STORAGE_PROJECT_VOLUME:
        # Pre-upgrade job: metadata was written into the project volume
        # itself, under the path an agent could also reach. Read it from
        # there for continuity; new jobs never take this branch.
        source_volume_name = volume.name
        source_mount = "/project"
        metadata_directory = LEGACY_COPY_METADATA_DIRECTORY
    else:
        return {}

    fields = ("status", "started_at", "finished_at", "exit_code", "error")
    commands = [
        (
            f"if [ -f {metadata_directory}/{field} ]; then "
            f"cat {metadata_directory}/{field}; fi; printf '\\n';"
        )
        for field in fields
    ]
    if include_logs:
        commands.append(
            f"if [ -f {metadata_directory}/copy.log ]; then "
            f"tail -c 8192 {metadata_directory}/copy.log; fi"
        )

    try:
        output = docker_client.containers.run(
            image=labels.get(LABEL_COPY_IMAGE, DEFAULT_COPY_IMAGE),
            command=["sh", "-c", "".join(commands)],
            remove=True,
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            labels={COPY_READER_LABEL: "true"},
            volumes={source_volume_name: {"bind": source_mount, "mode": "ro"}},
        )
    except NotFound:
        # The metadata volume (or, on the legacy path, the project volume)
        # is gone. Report unknown rather than crash the reader.
        return {}
    if isinstance(output, str):
        output_bytes = output.encode()
    else:
        output_bytes = output
    parts = output_bytes.split(b"\n", len(fields))
    parts.extend([b""] * (len(fields) + 1 - len(parts)))
    values = [part.decode("utf-8", errors="replace") for part in parts]
    metadata = dict(zip(fields, values[: len(fields)], strict=True))
    return {**metadata, "log": values[-1]}


def _copy_job_from_persisted(
    volume: Volume,
    labels: dict[str, str],
    persisted: dict[str, str],
) -> ProjectCopyJobStatus:
    status = persisted.get("status", "")
    error = persisted.get("error", "")
    if status not in {"completed", "failed"}:
        status = "unknown"
        if labels.get(LABEL_STATUS_STORAGE) in {
            STATUS_STORAGE_PROJECT_VOLUME,
            STATUS_STORAGE_CONTROLLER_VOLUME,
        }:
            error = "Copy container ended before it persisted a final status"
        else:
            error = "This legacy copy job has no persisted final status"

    exit_code_text = persisted.get("exit_code", "")
    exit_code = _integer(exit_code_text) if exit_code_text else None
    job_id = labels.get(LABEL_COPY_JOB_ID, "")
    return ProjectCopyJobStatus(
        job_id=job_id,
        sandbox_id=_sandbox_id(labels, volume.name),
        project_name=labels.get(LABEL_NAME, ""),
        source_path=labels.get(LABEL_SOURCE, ""),
        volume_name=volume.name,
        status=status,
        docker_status="removed",
        ready=status == "completed",
        created_at=labels.get(LABEL_CREATED_AT, ""),
        started_at=persisted.get("started_at", ""),
        finished_at=persisted.get("finished_at", ""),
        exit_code=exit_code,
        error=error,
        log_tail=persisted.get("log", ""),
        status_url=f"/projects/copies/{job_id}",
        excluded_directories=_excluded_directories(labels),
    )


def _copy_job_from_container(
    container: Container,
    *,
    include_logs: bool,
) -> ProjectCopyJobStatus:
    try:
        container.reload()
    except DockerException:
        pass

    attrs = container.attrs
    state = attrs.get("State") or {}
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    lifecycle_status = _lifecycle_status(state)
    docker_status = state.get("Status", container.status)
    log_tail = ""
    if include_logs:
        try:
            log_bytes = container.logs(
                stdout=True,
                stderr=True,
                tail=50,
                timestamps=True,
            )
        except DockerException as error:
            log_tail = f"Docker logs are unavailable: {error}"
        else:
            if isinstance(log_bytes, bytes):
                log_tail = log_bytes.decode("utf-8", errors="replace")[-8_192:]
            else:
                log_tail = str(log_bytes)[-8_192:]

    exit_code = (
        _integer(state.get("ExitCode"))
        if docker_status in {"exited", "dead"}
        else None
    )
    job_id = labels.get(LABEL_COPY_JOB_ID, "")
    return ProjectCopyJobStatus(
        job_id=job_id,
        sandbox_id=_sandbox_id(labels, _mounted_project_volume(attrs)),
        project_name=labels.get(LABEL_NAME, ""),
        source_path=labels.get(LABEL_SOURCE, ""),
        volume_name=_mounted_project_volume(attrs),
        status=lifecycle_status,
        docker_status=docker_status,
        ready=lifecycle_status == "completed",
        created_at=labels.get(LABEL_CREATED_AT, ""),
        started_at=_meaningful_time(state.get("StartedAt", "")),
        finished_at=_meaningful_time(state.get("FinishedAt", "")),
        exit_code=exit_code,
        error=state.get("Error", ""),
        log_tail=log_tail,
        status_url=f"/projects/copies/{job_id}",
        excluded_directories=_excluded_directories(labels),
    )


def _lifecycle_status(state: dict[str, Any]) -> str:
    docker_status = state.get("Status", "created")
    if docker_status == "created":
        return "queued"
    if docker_status in {"running", "restarting", "paused"}:
        return "copying"
    if docker_status == "exited" and _integer(state.get("ExitCode")) == 0:
        return "completed"
    return "failed"


def _mounted_project_volume(attrs: dict[str, Any]) -> str:
    for mount in attrs.get("Mounts", []):
        if mount.get("Type") == "volume" and mount.get("Destination") == "/project":
            return mount.get("Name", "")
    return ""


def _has_browsable_child(directory: Path, root: Path) -> bool:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    child = Path(entry.path).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if child.is_dir() and child.is_relative_to(root):
                    return True
    except OSError:
        return False
    return False


def _meaningful_time(value: Any) -> str:
    text = str(value or "")
    if not text or text.startswith("0001-01-01"):
        return ""
    return text


def _rollback_registration(
    volume: Volume | None,
    metadata_volume: Volume | None,
    helper: Container | None,
) -> None:
    if helper is not None:
        try:
            helper.remove(force=True)
        except DockerException:
            pass
    if volume is not None:
        try:
            volume.remove(force=True)
        except DockerException:
            pass
    if metadata_volume is not None:
        try:
            metadata_volume.remove(force=True)
        except DockerException:
            pass


def ensure_git_baseline(
    docker_client: DockerClient,
    git_image: str,
    volume_name: str,
) -> str:
    """Ensures the sandbox volume is a git repository with a baseline commit.

    Runs in a throwaway, hardened container using git_image, never the
    inspection or copy image: alpine:latest has no git, and git containers
    keep network_disabled=True, so git cannot be installed at runtime.
    A sandbox that already has commits keeps them; only the branch pointer
    and git identity are ensured. Returns the resulting HEAD commit hash.
    """
    _ensure_git_image(docker_client, git_image)
    output = docker_client.containers.run(
        image=git_image,
        entrypoint=["sh", "-c"],
        command=[GIT_BASELINE_SCRIPT],
        remove=True,
        network_disabled=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        volumes={volume_name: {"bind": "/project", "mode": "rw"}},
        tmpfs={"/tmp": "rw,nosuid,size=32m"},
    )
    return output.decode().strip().splitlines()[-1]


def _ensure_git_image(docker_client: DockerClient, image: str) -> None:
    try:
        docker_client.images.get(image)
    except ImageNotFound:
        docker_client.images.pull(image)


def _integer(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _excluded_directories(labels: dict[str, str]) -> list[str]:
    value = labels.get(LABEL_EXCLUDED_DIRECTORIES, "")
    return [name for name in value.split(",") if name]


def _sandbox_id(labels: dict[str, str], volume_name: str) -> str:
    value = labels.get(LABEL_SANDBOX_ID, "")
    if value:
        return value
    return uuid5(NAMESPACE_URL, f"orchestrator-sandbox:{volume_name}").hex


def project_id(source_path: str) -> str:
    return uuid5(NAMESPACE_URL, f"orchestrator-project:{source_path}").hex


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024

    raise AssertionError("unreachable")
