from collections.abc import Iterator
from pathlib import Path
from typing import Any

from docker.errors import ImageNotFound, NotFound
from fastapi.testclient import TestClient

from app.docker_client import get_docker_client
from app.main import app
from app.projects.config import ProjectSettings, get_project_settings
from app.projects.service import (
    COPY_CONTAINER_PREFIX,
    COPY_COMMAND,
    EXCLUDED_DIRECTORY_NAMES,
    LABEL_COPIED_BYTES,
    LABEL_COPY_JOB,
    LABEL_COPY_JOB_ID,
    LABEL_COPY_IMAGE,
    LABEL_COPY_MODE,
    LABEL_CREATED_AT,
    LABEL_EXCLUDED_DIRECTORIES,
    LABEL_FILE_COUNT,
    LABEL_MANAGED,
    LABEL_NAME,
    LABEL_SOURCE,
    LABEL_STATUS_STORAGE,
    STATUS_STORAGE_PROJECT_VOLUME,
)

client = TestClient(app)


class StubVolume:
    def __init__(self, name: str, labels: dict[str, str]) -> None:
        self.name = name
        self.attrs = {
            "Name": name,
            "Driver": "local",
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "Labels": labels,
        }
        self.removed_force: bool | None = None

    def remove(self, *, force: bool) -> None:
        self.removed_force = force


class StubVolumes:
    def __init__(self) -> None:
        self.items: list[StubVolume] = []
        self.create_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> list[StubVolume]:
        assert kwargs == {"filters": {"label": f"{LABEL_MANAGED}=true"}}
        return [
            volume
            for volume in self.items
            if volume.removed_force is None
            and (volume.attrs.get("Labels") or {}).get(LABEL_MANAGED) == "true"
        ]

    def get(self, volume_name: str) -> StubVolume:
        for volume in self.items:
            if volume.name == volume_name and volume.removed_force is None:
                return volume
        raise NotFound("volume not found")

    def create(self, **kwargs: Any) -> StubVolume:
        self.create_calls.append(kwargs)
        volume = StubVolume(kwargs["name"], kwargs["labels"])
        self.items.append(volume)
        return volume


class StubCopyHelper:
    def __init__(
        self,
        create_args: dict[str, Any],
        containers: "StubContainers",
    ) -> None:
        self.containers = containers
        self.name = create_args["name"]
        self.id = self.name
        self.short_id = self.name[:12]
        self.status = "created"
        self.removed_force: bool | None = None
        self.auto_removed = False
        self.log_output = b"copy started\n"
        self.attrs = {
            "Config": {"Labels": create_args["labels"]},
            "State": {
                "Status": "created",
                "ExitCode": 0,
                "Error": "",
                "StartedAt": "0001-01-01T00:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": source,
                    "Destination": config["bind"],
                }
                if Path(source).is_absolute()
                else {
                    "Type": "volume",
                    "Name": source,
                    "Destination": config["bind"],
                }
                for source, config in create_args["volumes"].items()
            ],
        }

    def start(self) -> None:
        self.status = "running"
        self.attrs["State"].update(
            {
                "Status": "running",
                "StartedAt": "2026-08-04T01:02:03Z",
            }
        )

    def complete(
        self,
        exit_code: int = 0,
        *,
        error: str = "",
        logs: bytes = b"copy started\ncopy completed\n",
    ) -> None:
        self.status = "exited"
        self.log_output = logs
        self.attrs["State"].update(
            {
                "Status": "exited",
                "ExitCode": exit_code,
                "Error": error,
                "FinishedAt": "2026-08-04T01:02:04Z",
            }
        )
        volume_name = next(
            source
            for source in self.containers.create_calls[-1]["volumes"]
            if not Path(source).is_absolute()
        )
        self.containers.persisted[volume_name] = {
            "status": "completed" if exit_code == 0 else "failed",
            "started_at": "2026-08-04T01:02:03Z",
            "finished_at": "2026-08-04T01:02:04Z",
            "exit_code": str(exit_code),
            "error": error,
            "log": logs.decode(),
        }
        self.auto_removed = True

    def reload(self) -> None:
        if self.auto_removed:
            raise NotFound("container was automatically removed")
        return None

    def logs(self, **kwargs: Any) -> bytes:
        assert kwargs == {
            "stdout": True,
            "stderr": True,
            "tail": 50,
            "timestamps": True,
        }
        return self.log_output

    def remove(self, *, force: bool) -> None:
        self.removed_force = force


class StubContainers:
    def __init__(self, *, image_available: bool = True) -> None:
        self.image_available = image_available
        self.items: list[StubCopyHelper] = []
        self.create_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.persisted: dict[str, dict[str, str]] = {}

    @property
    def helper(self) -> StubCopyHelper:
        return self.items[-1]

    def create(self, **kwargs: Any) -> StubCopyHelper:
        self.create_calls.append(kwargs)
        if not self.image_available:
            raise ImageNotFound("copy image not found")
        helper = StubCopyHelper(kwargs, self)
        self.items.append(helper)
        return helper

    def get(self, name: str) -> StubCopyHelper:
        for helper in self.items:
            if (
                helper.name == name
                and helper.removed_force is None
                and not helper.auto_removed
            ):
                return helper
        raise NotFound("container not found")

    def run(self, **kwargs: Any) -> bytes:
        self.run_calls.append(kwargs)
        volume_name = next(iter(kwargs["volumes"]))
        persisted = self.persisted.get(volume_name, {})
        fields = ("status", "started_at", "finished_at", "exit_code", "error")
        output = "\n".join(persisted.get(field, "") for field in fields) + "\n"
        if "copy.log" in kwargs["command"][2]:
            output += persisted.get("log", "")
        return output.encode()

    def list(self, **kwargs: Any) -> list[StubCopyHelper]:
        assert kwargs == {
            "all": True,
            "filters": {"label": f"{LABEL_COPY_JOB}=true"},
        }
        return [helper for helper in self.items if helper.removed_force is None]


class StubDockerClient:
    def __init__(self, *, image_available: bool = True) -> None:
        self.volumes = StubVolumes()
        self.containers = StubContainers(image_available=image_available)


def _override_docker(docker_client: StubDockerClient) -> Any:
    def override() -> Iterator[StubDockerClient]:
        yield docker_client

    return override


def _override_settings(projects_root: Path) -> Any:
    def override() -> ProjectSettings:
        return ProjectSettings(
            projects_root=projects_root,
            copy_image="alpine:latest",
        )

    return override


def _set_overrides(docker_client: StubDockerClient, projects_root: Path) -> None:
    app.dependency_overrides[get_docker_client] = _override_docker(docker_client)
    app.dependency_overrides[get_project_settings] = _override_settings(projects_root)


def _register(
    docker_client: StubDockerClient,
    projects_root: Path,
    source: Path,
) -> Any:
    _set_overrides(docker_client, projects_root)
    return client.post("/projects", json={"path": str(source)})


def test_register_and_observe_copy_lifecycle(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    source = projects_root / "sample-project"
    source.mkdir(parents=True)
    (source / "README.md").write_text("hello", encoding="utf-8")
    (source / ".env.example").write_text("DEBUG=0", encoding="utf-8")
    excluded_files = {
        source / "node_modules" / "package" / "index.js": "dependency",
        source / "dist" / "bundle.js": "generated bundle",
        source / ".venv" / "lib" / "python.py": "installed package",
        source / ".orchestrator" / "old-status": "reserved metadata",
        source / "src" / "build" / "output.js": "generated output",
    }
    for excluded_file, contents in excluded_files.items():
        excluded_file.parent.mkdir(parents=True, exist_ok=True)
        excluded_file.write_text(contents, encoding="utf-8")
    external_file = tmp_path / "external.txt"
    external_file.write_text("not inventoried", encoding="utf-8")
    (source / "external-link").symlink_to(external_file)

    docker_client = StubDockerClient()
    try:
        created = _register(docker_client, projects_root, source)
        job = created.json()
        helper = docker_client.containers.helper
        copying_detail = client.get(job["status_url"])
        helper.complete()
        completed_detail = client.get(job["status_url"])
        jobs = client.get("/projects/copies")
        listed = client.get("/projects")
        inspected = client.get("/projects/sample-project-sandbox-1")
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 202
    assert job["project_name"] == "sample-project-sandbox-1"
    assert job["status"] == "copying"
    assert job["docker_status"] == "running"
    assert job["ready"] is False
    assert job["status_url"] == f"/projects/copies/{job['job_id']}"
    assert job["volume_name"].startswith("orchestrator-project-sample-project-")
    assert job["excluded_directories"] == list(EXCLUDED_DIRECTORY_NAMES)

    assert copying_detail.status_code == 200
    assert copying_detail.json()["status"] == "copying"
    assert "copy started" in copying_detail.json()["log_tail"]

    completed = completed_detail.json()
    assert completed_detail.status_code == 200
    assert completed["status"] == "completed"
    assert completed["docker_status"] == "removed"
    assert completed["ready"] is True
    assert completed["exit_code"] == 0
    assert completed["finished_at"] == "2026-08-04T01:02:04Z"
    assert "copy completed" in completed["log_tail"]

    assert jobs.status_code == 200
    assert jobs.json()["count"] == 1
    assert jobs.json()["jobs"][0]["status"] == "completed"
    assert jobs.json()["jobs"][0]["log_tail"] == ""

    project = listed.json()["projects"][0]
    assert listed.status_code == 200
    assert project["name"] == "sample-project-sandbox-1"
    assert project["file_count"] == 2
    assert project["copied_bytes"] == 12
    assert project["copied_size"] == "12 B"
    assert project["copy_job_id"] == job["job_id"]
    assert project["copy_status"] == "completed"
    assert project["ready"] is True
    assert inspected.json() == project

    volume_labels = docker_client.volumes.create_calls[0]["labels"]
    assert volume_labels[LABEL_MANAGED] == "true"
    assert volume_labels[LABEL_NAME] == "sample-project-sandbox-1"
    assert volume_labels[LABEL_SOURCE] == str(source)
    assert volume_labels[LABEL_COPY_MODE] == "snapshot"
    assert volume_labels[LABEL_FILE_COUNT] == "2"
    assert volume_labels[LABEL_COPIED_BYTES] == "12"
    assert volume_labels[LABEL_EXCLUDED_DIRECTORIES] == ",".join(
        EXCLUDED_DIRECTORY_NAMES
    )
    assert volume_labels[LABEL_CREATED_AT].endswith("Z")
    assert volume_labels[LABEL_COPY_JOB_ID] == job["job_id"]
    assert volume_labels[LABEL_COPY_IMAGE] == "alpine:latest"
    assert volume_labels[LABEL_STATUS_STORAGE] == STATUS_STORAGE_PROJECT_VOLUME

    helper_call = docker_client.containers.create_calls[0]
    assert helper_call["image"] == "alpine:latest"
    assert helper_call["name"] == f"{COPY_CONTAINER_PREFIX}{job['job_id']}"
    assert helper_call["network_disabled"] is True
    assert helper_call["read_only"] is True
    assert helper_call["cap_drop"] == ["ALL"]
    assert helper_call["security_opt"] == ["no-new-privileges:true"]
    assert helper_call["auto_remove"] is True
    copy_script = COPY_COMMAND[2]
    for directory_name in EXCLUDED_DIRECTORY_NAMES:
        assert f"--exclude='./{directory_name}'" in copy_script
        assert f"--exclude='*/{directory_name}'" in copy_script
    assert helper_call["volumes"] == {
        str(source): {"bind": "/source", "mode": "ro"},
        job["volume_name"]: {"bind": "/project", "mode": "rw"},
    }
    assert helper.auto_removed is True
    assert helper.removed_force is None
    assert docker_client.containers.run_calls
    reader_call = docker_client.containers.run_calls[0]
    assert reader_call["remove"] is True
    assert reader_call["volumes"] == {
        job["volume_name"]: {"bind": "/project", "mode": "ro"}
    }


def test_failed_copy_is_reported(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    source = projects_root / "sample-project"
    source.mkdir(parents=True)
    docker_client = StubDockerClient()

    try:
        created = _register(docker_client, projects_root, source)
        docker_client.containers.helper.complete(
            2,
            error="copy command failed",
            logs=b"copy started\ntar: permission denied\n",
        )
        detail = client.get(created.json()["status_url"])
        project = client.get("/projects/sample-project-sandbox-1")
    finally:
        app.dependency_overrides.clear()

    assert detail.status_code == 200
    assert detail.json()["status"] == "failed"
    assert detail.json()["ready"] is False
    assert detail.json()["exit_code"] == 2
    assert detail.json()["error"] == "copy command failed"
    assert "permission denied" in detail.json()["log_tail"]
    assert project.json()["copy_status"] == "failed"


def test_copy_job_not_found(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    projects_root.mkdir()
    docker_client = StubDockerClient()
    _set_overrides(docker_client, projects_root)

    try:
        response = client.get("/projects/copies/missing")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Copy job 'missing' was not found"}


def test_browse_project_folders_stays_inside_root(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    nested = projects_root / "Alpha" / "child"
    nested.mkdir(parents=True)
    (projects_root / "beta").mkdir()
    (projects_root / "file.txt").write_text("ignored", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (projects_root / "escape").symlink_to(outside, target_is_directory=True)
    docker_client = StubDockerClient()
    _set_overrides(docker_client, projects_root)

    try:
        root_response = client.get("/projects/browse")
        nested_response = client.get(
            "/projects/browse",
            params={"path": str(projects_root / "Alpha")},
        )
        outside_response = client.get(
            "/projects/browse",
            params={"path": str(outside)},
        )
    finally:
        app.dependency_overrides.clear()

    assert root_response.status_code == 200
    assert root_response.json() == {
        "root": str(projects_root),
        "path": str(projects_root),
        "parent": None,
        "entries": [
            {
                "name": "Alpha",
                "path": str(projects_root / "Alpha"),
                "has_children": True,
            },
            {
                "name": "beta",
                "path": str(projects_root / "beta"),
                "has_children": False,
            },
        ],
    }
    assert nested_response.status_code == 200
    assert nested_response.json()["parent"] == str(projects_root)
    assert nested_response.json()["entries"][0]["name"] == "child"
    assert outside_response.status_code == 400


def test_registration_rejects_paths_outside_root(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    projects_root.mkdir()
    outside = tmp_path / "private-project"
    outside.mkdir()
    escape = projects_root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    docker_client = StubDockerClient()
    _set_overrides(docker_client, projects_root)

    try:
        responses = [
            client.post("/projects", json={"path": str(outside)}),
            client.post("/projects", json={"path": str(escape)}),
            client.post(
                "/projects",
                json={"path": str(projects_root)},
            ),
        ]
    finally:
        app.dependency_overrides.clear()

    expected_detail = f"Project folder must be inside '{projects_root}'"
    assert all(response.status_code == 400 for response in responses)
    assert all(response.json() == {"detail": expected_detail} for response in responses)
    assert docker_client.volumes.create_calls == []


def test_copying_one_folder_creates_incrementing_sandboxes(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    source = projects_root / "sample-project"
    source.mkdir(parents=True)
    docker_client = StubDockerClient()

    try:
        first = _register(docker_client, projects_root, source)
        second = _register(docker_client, projects_root, source)
        third = _register(docker_client, projects_root, source)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 202
    assert first.json()["project_name"] == "sample-project-sandbox-1"
    assert second.json()["project_name"] == "sample-project-sandbox-2"
    assert third.json()["project_name"] == "sample-project-sandbox-3"
    assert len(docker_client.volumes.create_calls) == 3
    assert len({call["name"] for call in docker_client.volumes.create_calls}) == 3
    assert all(
        call["labels"][LABEL_SOURCE] == str(source)
        for call in docker_client.volumes.create_calls
    )


def test_missing_copy_image_rolls_back_volume(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    source = projects_root / "sample-project"
    source.mkdir(parents=True)
    docker_client = StubDockerClient(image_available=False)

    try:
        response = _register(docker_client, projects_root, source)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 424
    assert response.json() == {
        "detail": "Project copy image 'alpine:latest' is not available"
    }
    assert docker_client.volumes.items[0].removed_force is True


def test_folder_name_is_normalized_for_sandbox_name(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    source = projects_root / "@sample(project)"
    source.mkdir(parents=True)
    docker_client = StubDockerClient()
    _set_overrides(docker_client, projects_root)

    try:
        response = client.post("/projects", json={"path": str(source)})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["project_name"] == "sample-project-sandbox-1"


def test_long_folder_name_creates_unique_sandbox_volumes(tmp_path: Path) -> None:
    projects_root = tmp_path / "Documents"
    source = projects_root / ("long-project-name-" * 6)
    source.mkdir(parents=True)
    docker_client = StubDockerClient()

    try:
        first = _register(docker_client, projects_root, source)
        second = _register(docker_client, projects_root, source)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(first.json()["project_name"]) <= 100
    assert len(second.json()["project_name"]) <= 100
    assert first.json()["project_name"].endswith("-sandbox-1")
    assert second.json()["project_name"].endswith("-sandbox-2")
    assert first.json()["volume_name"] != second.json()["volume_name"]
