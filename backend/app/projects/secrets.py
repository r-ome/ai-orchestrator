from typing import Any

from docker.client import DockerClient

from app.controller.store import ControllerStore
from app.previews._shared import _project_key, _ready_project
from app.previews.config import PreviewSettings
from app.previews.dependency_cache import _volume_environment_files
from app.previews.detection import parse_environment_pairs
from app.previews.models import (
    ENVIRONMENT_VARIABLE_PATTERN,
    ImportProjectSecretsResponse,
    ProjectSecretName,
    ProjectSecrets,
    SetProjectSecretsRequest,
)


def get_project_secrets(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> ProjectSecrets:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    return _project_secrets_response(controller_store, project, project_key)


def set_project_secrets(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    request: SetProjectSecretsRequest,
) -> ProjectSecrets:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    controller_store.set_project_secrets(project_key, request.values)
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=None,
        kind="preview.secrets.set",
        payload={"names": sorted(request.values)},
    )
    return _project_secrets_response(controller_store, project, project_key)


def delete_project_secret(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
    name: str,
) -> ProjectSecrets:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    controller_store.delete_project_secret(project_key, name)
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=None,
        kind="preview.secrets.deleted",
        payload={"names": [name]},
    )
    return _project_secrets_response(controller_store, project, project_key)


def import_project_secrets(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PreviewSettings,
    project_name: str,
) -> ImportProjectSecretsResponse:
    project = _ready_project(docker_client, project_name, controller_store)
    project_key = _project_key(project)
    environment_files = _volume_environment_files(
        docker_client,
        project.volume_name,
        settings,
    )
    pairs = parse_environment_pairs(environment_files)
    imported: list[str] = []
    skipped: list[str] = []
    values: dict[str, str] = {}
    for name, value in pairs.items():
        if not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(name) or len(
            value.encode("utf-8")
        ) > 8_192:
            skipped.append(name)
            continue
        values[name] = value
        imported.append(name)
    if values:
        controller_store.set_project_secrets(project_key, values)
    controller_store.event(
        sandbox_id=project.sandbox_id,
        run_id=None,
        kind="preview.secrets.imported",
        payload={"imported": sorted(imported), "skipped": sorted(skipped)},
    )
    return ImportProjectSecretsResponse(
        project_name=project.name,
        imported=sorted(imported),
        skipped=sorted(skipped),
    )


def _project_secrets_response(
    controller_store: ControllerStore,
    project: Any,
    project_key: str,
) -> ProjectSecrets:
    return ProjectSecrets(
        project_name=project.name,
        names=[
            ProjectSecretName(name=entry["name"], updated_at=entry["updated_at"])
            for entry in controller_store.project_secret_entries(project_key)
        ],
    )
