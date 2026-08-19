from uuid import NAMESPACE_URL, uuid5

import pytest

from app.platform.naming import (
    agent_container,
    database_name,
    db_data_volume,
    feature_branch,
    network,
    ownership_labels,
    sandbox_id_for,
    short_id,
    validate_feature_key,
    validate_ownership,
    workspace_volume,
)

PROJECT_ID = "a" * 32
FEATURE_KEY = "add-sandbox-manifest"


def test_sandbox_id_uses_uuid_hex_and_is_deterministic() -> None:
    sandbox_id = sandbox_id_for(PROJECT_ID, FEATURE_KEY)

    assert sandbox_id == uuid5(NAMESPACE_URL, f"{PROJECT_ID}:{FEATURE_KEY}").hex
    assert len(sandbox_id) == 32
    assert sandbox_id == sandbox_id_for(PROJECT_ID, FEATURE_KEY)
    assert sandbox_id != sandbox_id_for(PROJECT_ID, "add-sandbox-naming")


def test_resource_names_use_only_the_short_id() -> None:
    sandbox_id = sandbox_id_for(PROJECT_ID, FEATURE_KEY)
    short = short_id(sandbox_id)

    assert workspace_volume(sandbox_id) == f"sbx-{short}-ws"
    assert agent_container(sandbox_id) == f"sbx-{short}-agent"
    assert network(sandbox_id) == f"sbx-{short}-net"
    assert db_data_volume(sandbox_id) == f"sbx-{short}-db"
    assert database_name(sandbox_id) == f"sbx_{short}"
    assert feature_branch(FEATURE_KEY) == f"feature/{FEATURE_KEY}"
    assert sandbox_id not in {
        workspace_volume(sandbox_id),
        agent_container(sandbox_id),
        network(sandbox_id),
        db_data_volume(sandbox_id),
        database_name(sandbox_id),
    }


def test_ownership_labels_keep_the_full_identity() -> None:
    sandbox_id = sandbox_id_for(PROJECT_ID, FEATURE_KEY)

    labels = ownership_labels(sandbox_id=sandbox_id, project_id=PROJECT_ID)

    assert labels == {
        "orchestrator.sandbox.id": sandbox_id,
        "orchestrator.project.id": PROJECT_ID,
        "orchestrator.lifecycle.version": "v1",
    }


@pytest.mark.parametrize(
    "feature_key",
    ["ab", "feature-1", "a" + "-b" * 31],
)
def test_validate_feature_key_accepts_v1_keys(feature_key: str) -> None:
    assert validate_feature_key(feature_key) == feature_key


@pytest.mark.parametrize(
    "feature_key",
    ["", "a", "Upper-case", "has_space", "has/slash", "a" * 65, "-starts-dash"],
)
def test_validate_feature_key_rejects_invalid_v1_keys(feature_key: str) -> None:
    with pytest.raises(ValueError, match="feature_key"):
        validate_feature_key(feature_key)


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {"orchestrator.sandbox.id": "wrong"},
        {
            "orchestrator.sandbox.id": sandbox_id_for(PROJECT_ID, FEATURE_KEY),
            "orchestrator.project.id": PROJECT_ID,
            "orchestrator.lifecycle.version": "legacy",
        },
    ],
)
def test_validate_ownership_refuses_name_matches_without_valid_labels(
    labels: dict[str, str],
) -> None:
    sandbox_id = sandbox_id_for(PROJECT_ID, FEATURE_KEY)
    resource = {"name": workspace_volume(sandbox_id), "labels": labels}

    with pytest.raises(ValueError, match="ownership"):
        validate_ownership(resource, sandbox_id=sandbox_id)


def test_validate_ownership_accepts_a_matching_named_resource() -> None:
    sandbox_id = sandbox_id_for(PROJECT_ID, FEATURE_KEY)
    resource = {
        "name": workspace_volume(sandbox_id),
        "labels": ownership_labels(sandbox_id=sandbox_id, project_id=PROJECT_ID),
    }

    validate_ownership(resource, sandbox_id=sandbox_id)
