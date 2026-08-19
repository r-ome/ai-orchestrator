import pytest
from conftest import FakeDockerClient
from docker.errors import APIError, ImageNotFound, NotFound


def test_label_filtering_requires_every_requested_label(
    fake_docker_client: FakeDockerClient,
) -> None:
    matching = fake_docker_client.containers.create(
        name="matching",
        image="test-image",
        labels={"managed": "true", "sandbox": "one"},
    )
    fake_docker_client.containers.create(
        name="partial",
        image="test-image",
        labels={"managed": "true"},
    )

    resources = fake_docker_client.containers.list(
        all=True,
        filters={"label": ["managed=true", "sandbox=one"]},
    )

    assert resources == [matching]


def test_created_and_removed_resources_are_recorded() -> None:
    docker_client = FakeDockerClient()
    volume = docker_client.volumes.create(
        name="project-data",
        labels={"managed": "true"},
    )

    volume.remove(force=True)

    assert docker_client.created == [volume]
    assert docker_client.removed == [volume]
    assert docker_client.volumes.list() == []


def test_injected_failure_raises_the_supplied_docker_error(
    fake_docker_client: FakeDockerClient,
) -> None:
    failure = APIError("Docker daemon is unavailable")
    fake_docker_client.inject_failure("networks.create", failure)

    with pytest.raises(APIError) as error:
        fake_docker_client.networks.create("sandbox-network")

    assert error.value is failure


def test_missing_resources_raise_real_docker_errors(
    fake_docker_client: FakeDockerClient,
) -> None:
    with pytest.raises(NotFound):
        fake_docker_client.volumes.get("missing-volume")
    with pytest.raises(ImageNotFound):
        fake_docker_client.images.get("missing-image")
