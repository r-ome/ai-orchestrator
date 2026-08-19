from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.controller.store import ControllerStore
from app.previews.detection import parse_environment_pairs, proposal_digest
from app.previews.errors import PreviewOperationError
from app.previews.models import (
    ImportProjectSecretsResponse,
    PreviewConfiguration,
    PreviewEnvironmentSource,
    PreviewMode,
    PreviewNetworkAccess,
)
from app.projects.secrets import _project_secrets_response
from app.previews.runtimes.compose import _compose_service_environment
from app.previews.runtimes.environment import _secret_environment
from app.previews.service import _environment_status


ATC_DOTENV = b"""
# DATABASE_URL="mysql://old-user:old-pass@localhost:3306/atc"
DATABASE_URL="mysql://real-user:real-pass@localhost:3306/atc"
# DATABASE_URL="mysql://another-user:another-pass@localhost:3306/atc"
export NEXTAUTH_SECRET=supersecret
NEXTAUTH_URL='http://localhost:3000'
AWS_REGION=us-east-1
AWS_S3_CONTAINER_REPORTS_BUCKET=atc-reports
"""


def test_environment_status_computes_configured_and_missing() -> None:
    configured, missing = _environment_status(
        ["DATABASE_URL", "NEXTAUTH_SECRET", "AWS_REGION"],
        secret_names={"NEXTAUTH_SECRET"},
        controller_managed={"DATABASE_URL"},
    )

    assert configured == ["DATABASE_URL", "NEXTAUTH_SECRET"]
    assert missing == ["AWS_REGION"]


def test_preview_environment_source_accepts_from_secret_for_any_name() -> None:
    source = PreviewEnvironmentSource(from_secret="NEXTAUTH_SECRET")
    assert source.from_secret == "NEXTAUTH_SECRET"


def test_preview_environment_source_rejects_both_fields_set() -> None:
    with pytest.raises(ValidationError):
        PreviewEnvironmentSource(from_service="database", from_secret="NEXTAUTH_SECRET")


def test_preview_environment_source_rejects_neither_field_set() -> None:
    with pytest.raises(ValidationError):
        PreviewEnvironmentSource()


def test_compose_config_with_only_from_secret_environment_validates() -> None:
    config = PreviewConfiguration(
        mode=PreviewMode.COMPOSE,
        compose_file="compose.yaml",
        selected_service="web",
        container_port=3000,
        network_access=PreviewNetworkAccess.ISOLATED,
        environment={
            "NEXTAUTH_SECRET": PreviewEnvironmentSource(from_secret="NEXTAUTH_SECRET"),
        },
    )

    assert config.environment["NEXTAUTH_SECRET"].from_secret == "NEXTAUTH_SECRET"


def test_compose_config_with_services_is_rejected() -> None:
    with pytest.raises(ValidationError, match="native previews"):
        PreviewConfiguration(
            mode=PreviewMode.COMPOSE,
            compose_file="compose.yaml",
            selected_service="web",
            container_port=3000,
            network_access=PreviewNetworkAccess.ISOLATED,
            services={
                "database": {
                    "type": "mysql",
                    "image": "mysql:8.4",
                    "database": "atc_preview",
                }
            },
        )


def test_secret_environment_raises_422_naming_missing_variable() -> None:
    config = PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        image="node:22-alpine",
        start_command="npm run dev",
        container_port=3000,
        network_access=PreviewNetworkAccess.ISOLATED,
        environment={
            "NEXTAUTH_SECRET": PreviewEnvironmentSource(from_secret="NEXTAUTH_SECRET"),
        },
    )

    with pytest.raises(PreviewOperationError) as excinfo:
        _secret_environment(config, {})

    assert "NEXTAUTH_SECRET" in excinfo.value.detail
    assert excinfo.value.status_code == 422


def test_secret_environment_injects_stored_value() -> None:
    config = PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        image="node:22-alpine",
        start_command="npm run dev",
        container_port=3000,
        network_access=PreviewNetworkAccess.ISOLATED,
        environment={
            "NEXTAUTH_SECRET": PreviewEnvironmentSource(from_secret="NEXTAUTH_SECRET"),
        },
    )

    environment = _secret_environment(config, {"NEXTAUTH_SECRET": "shh"})

    assert environment == {"NEXTAUTH_SECRET": "shh"}


def _base_config(**overrides: object) -> PreviewConfiguration:
    values: dict[str, object] = {
        "mode": PreviewMode.NATIVE,
        "image": "node:22-alpine",
        "start_command": "npm run dev",
        "container_port": 3000,
        "network_access": PreviewNetworkAccess.ISOLATED,
    }
    values.update(overrides)
    return PreviewConfiguration(**values)


def test_digest_changes_when_variable_name_added() -> None:
    base = _base_config()
    with_variable = _base_config(
        environment={"NEXTAUTH_SECRET": PreviewEnvironmentSource(from_secret="NEXTAUTH_SECRET")}
    )

    assert proposal_digest(base, {}) != proposal_digest(with_variable, {})


def test_digest_unchanged_when_only_secret_value_changes() -> None:
    """The digest never sees secret values, so rotating one must not move it."""
    config = _base_config(
        environment={"NEXTAUTH_SECRET": PreviewEnvironmentSource(from_secret="NEXTAUTH_SECRET")}
    )

    digest_before = proposal_digest(config, {})
    # Rotating a stored secret's value never touches config or protected hashes.
    digest_after = proposal_digest(config, {})

    assert digest_before == digest_after


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    return store


def test_store_project_secrets_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_project_secrets("project-1", {"A": "1", "B": "2"})

    assert store.project_secret_names("project-1") == ["A", "B"]
    assert store.project_secrets("project-1") == {"A": "1", "B": "2"}

    entries = store.project_secret_entries("project-1")
    assert [entry["name"] for entry in entries] == ["A", "B"]
    assert all("updated_at" in entry for entry in entries)

    store.delete_project_secret("project-1", "A")
    assert store.project_secret_names("project-1") == ["B"]


def test_store_project_secrets_are_scoped_per_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_project_secrets("project-1", {"A": "1"})
    store.set_project_secrets("project-2", {"A": "2"})

    assert store.project_secrets("project-1") == {"A": "1"}
    assert store.project_secrets("project-2") == {"A": "2"}


def test_store_set_project_secrets_upserts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_project_secrets("project-1", {"A": "1"})
    store.set_project_secrets("project-1", {"A": "updated"})

    assert store.project_secrets("project-1") == {"A": "updated"}


def test_secrets_response_body_never_contains_a_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_project_secrets("project-1", {"NEXTAUTH_SECRET": "super-secret-value"})
    project = SimpleNamespace(name="demo")

    response = _project_secrets_response(store, project, "project-1")
    body = response.model_dump_json()

    assert "super-secret-value" not in body
    assert response.names[0].name == "NEXTAUTH_SECRET"


def test_import_response_body_never_contains_a_value() -> None:
    pairs = parse_environment_pairs({".env": ATC_DOTENV})

    response = ImportProjectSecretsResponse(
        project_name="demo",
        imported=sorted(pairs),
        skipped=[],
    )
    body = response.model_dump_json()

    for value in pairs.values():
        assert value not in body


def test_compose_service_environment_injects_into_selected_service_only() -> None:
    declared = {"NODE_ENV": "production"}
    application = {"NEXTAUTH_SECRET": "shh"}

    selected = _compose_service_environment(declared, application, selected=True)
    sidecar = _compose_service_environment(declared, application, selected=False)

    assert selected == {"NODE_ENV": "production", "NEXTAUTH_SECRET": "shh"}
    assert sidecar == {"NODE_ENV": "production"}


def test_compose_service_environment_lets_secrets_override_declared_values() -> None:
    environment = _compose_service_environment(
        {"NEXTAUTH_URL": "http://placeholder"},
        {"NEXTAUTH_URL": "http://localhost:3000"},
        selected=True,
    )

    assert environment["NEXTAUTH_URL"] == "http://localhost:3000"
