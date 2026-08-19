from collections import defaultdict, deque
from collections.abc import Iterator
from typing import Any

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

from app.controller.config import get_controller_settings
from app.controller.store import ControllerStore
from app.sandboxes.manifest import (
    SandboxManifest,
    transition_sandbox_lifecycle,
    write_manifest,
)
from app.sandboxes.models import SandboxLifecycleStatus


class FakeDockerResource:
    """A Docker resource whose state is sufficient for service-level tests."""

    def __init__(
        self,
        client: "FakeDockerClient",
        *,
        kind: str,
        name: str,
        labels: dict[str, str] | None = None,
        resource_id: str | None = None,
    ) -> None:
        self._client = client
        self._kind = kind
        self.name = name
        self.id = resource_id or name
        self.labels = dict(labels or {})
        self.removed = False
        self.attrs: dict[str, Any] = {"Name": name, "Id": self.id, "Labels": self.labels}

    def remove(self, **_: Any) -> None:
        self._client._raise_if_failed(f"{self._kind[:-1]}.remove")
        if self.removed:
            raise NotFound(f"{self._kind[:-1]} '{self.name}' was not found")
        self.removed = True
        self._client.removed.append(self)

    def reload(self) -> None:
        self._client._raise_if_failed(f"{self._kind[:-1]}.reload")
        if self.removed:
            raise NotFound(f"{self._kind[:-1]} '{self.name}' was not found")


class FakeDockerContainer(FakeDockerResource):
    def __init__(
        self,
        client: "FakeDockerClient",
        create_args: dict[str, Any],
        number: int,
    ) -> None:
        name = str(create_args.get("name") or f"container-{number:04d}")
        super().__init__(
            client,
            kind="containers",
            name=name,
            labels=create_args.get("labels"),
            resource_id=f"container-{number:04d}",
        )
        self.short_id = self.id[:12]
        self.status = "created"
        self.exit_code = 0
        self.log_output = b""
        self.attrs = {
            "Id": self.id,
            "Name": self.name,
            "Config": {
                "Image": create_args.get("image"),
                "Labels": self.labels,
            },
            "State": {"Status": self.status, "ExitCode": self.exit_code},
        }

    def start(self) -> None:
        self._client._raise_if_failed("container.start")
        self.status = "running"
        self.attrs["State"]["Status"] = self.status

    def stop(self, *, timeout: int | None = None) -> None:
        self._client._raise_if_failed("container.stop")
        self.status = "exited"
        self.attrs["State"].update({"Status": self.status, "ExitCode": self.exit_code})

    def wait(self, **_: Any) -> dict[str, int]:
        self._client._raise_if_failed("container.wait")
        self.status = "exited"
        self.attrs["State"].update({"Status": self.status, "ExitCode": self.exit_code})
        return {"StatusCode": self.exit_code}

    def logs(self, **_: Any) -> bytes:
        self._client._raise_if_failed("container.logs")
        return self.log_output


class FakeDockerNetwork(FakeDockerResource):
    def __init__(
        self,
        client: "FakeDockerClient",
        *,
        name: str,
        labels: dict[str, str] | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(
            client,
            kind="networks",
            name=name,
            labels=labels,
            resource_id=resource_id,
        )
        self.connections: list[tuple[Any, dict[str, Any]]] = []

    def connect(self, container: Any, **kwargs: Any) -> None:
        self._client._raise_if_failed("network.connect")
        self.connections.append((container, kwargs))

    def disconnect(self, container: Any, **kwargs: Any) -> None:
        self._client._raise_if_failed("network.disconnect")
        for index, (connected, _) in enumerate(self.connections):
            if connected is container or connected == container:
                self.connections.pop(index)
                return
        raise APIError(f"container is not connected to network '{self.name}'")


class FakeDockerImage(FakeDockerResource):
    def __init__(
        self,
        client: "FakeDockerClient",
        *,
        name: str,
        labels: dict[str, str] | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(
            client,
            kind="images",
            name=name,
            labels=labels,
            resource_id=resource_id,
        )
        self.tags = [name]
        self.attrs = {
            "Id": self.id,
            "RepoTags": self.tags,
            "Config": {"Labels": self.labels},
            "Labels": self.labels,
            "Size": 0,
        }


class _FakeDockerCollection:
    def __init__(self, client: "FakeDockerClient", kind: str) -> None:
        self.client = client
        self.kind = kind
        self.items: list[FakeDockerResource] = []

    def _active(self) -> list[FakeDockerResource]:
        return [resource for resource in self.items if not resource.removed]

    def _matches(self, resource: FakeDockerResource, filters: dict[str, Any] | None) -> bool:
        if not filters:
            return True
        requested_labels = filters.get("label", [])
        if isinstance(requested_labels, str):
            requested_labels = [requested_labels]
        for label in requested_labels:
            key, separator, value = str(label).partition("=")
            if key not in resource.labels:
                return False
            if separator and resource.labels[key] != value:
                return False
        names = filters.get("name", filters.get("names", []))
        if isinstance(names, str):
            names = [names]
        return not names or resource.name in names or resource.id in names

    def list(self, *, filters: dict[str, Any] | None = None, **_: Any) -> list[FakeDockerResource]:
        self.client._raise_if_failed(f"{self.kind}.list")
        return [resource for resource in self._active() if self._matches(resource, filters)]

    def get(self, identifier: str) -> FakeDockerResource:
        self.client._raise_if_failed(f"{self.kind}.get")
        for resource in self._active():
            if identifier in {resource.name, resource.id}:
                return resource
        raise NotFound(f"{self.kind[:-1]} '{identifier}' was not found")


class _FakeDockerContainers(_FakeDockerCollection):
    def __init__(self, client: "FakeDockerClient") -> None:
        super().__init__(client, "containers")

    def create(self, **kwargs: Any) -> FakeDockerContainer:
        self.client._raise_if_failed("containers.create")
        container = FakeDockerContainer(self.client, kwargs, len(self.items) + 1)
        self.items.append(container)
        self.client.created.append(container)
        return container

    def run(self, **kwargs: Any) -> bytes | FakeDockerContainer:
        self.client._raise_if_failed("containers.run")
        container = self.create(**kwargs)
        container.start()
        container.wait()
        if kwargs.get("detach"):
            return container
        if kwargs.get("remove"):
            container.remove(force=True)
        return container.log_output

    def list(
        self,
        *,
        all: bool = False,
        filters: dict[str, Any] | None = None,
        **_: Any,
    ) -> list[FakeDockerContainer]:
        self.client._raise_if_failed("containers.list")
        containers = [
            resource
            for resource in self._active()
            if all or isinstance(resource, FakeDockerContainer) and resource.status == "running"
        ]
        return [
            resource
            for resource in containers
            if self._matches(resource, filters)
        ]  # type: ignore[return-value]

    def get(self, identifier: str) -> FakeDockerContainer:
        self.client._raise_if_failed("containers.get")
        for container in self._active():
            if identifier in {container.name, container.id, getattr(container, "short_id", "")}:
                return container  # type: ignore[return-value]
        raise NotFound(f"container '{identifier}' was not found")

    def prune(self, **_: Any) -> dict[str, list[str]]:
        self.client._raise_if_failed("containers.prune")
        removed = []
        for container in self._active():
            if isinstance(container, FakeDockerContainer) and container.status != "running":
                container.remove(force=True)
                removed.append(container.id)
        return {"ContainersDeleted": removed}


class _FakeDockerVolumes(_FakeDockerCollection):
    def __init__(self, client: "FakeDockerClient") -> None:
        super().__init__(client, "volumes")

    def create(self, **kwargs: Any) -> FakeDockerResource:
        self.client._raise_if_failed("volumes.create")
        volume = FakeDockerResource(
            self.client,
            kind="volumes",
            name=kwargs["name"],
            labels=kwargs.get("labels"),
        )
        volume.attrs.update({"Driver": kwargs.get("driver", "local")})
        self.items.append(volume)
        self.client.created.append(volume)
        return volume

    def prune(self, **_: Any) -> dict[str, list[str]]:
        self.client._raise_if_failed("volumes.prune")
        removed = []
        for volume in self._active():
            volume.remove(force=True)
            removed.append(volume.name)
        return {"VolumesDeleted": removed}


class _FakeDockerNetworks(_FakeDockerCollection):
    def __init__(self, client: "FakeDockerClient") -> None:
        super().__init__(client, "networks")

    def create(self, name: str, **kwargs: Any) -> FakeDockerNetwork:
        self.client._raise_if_failed("networks.create")
        network = FakeDockerNetwork(
            self.client,
            name=name,
            labels=kwargs.get("labels"),
            resource_id=f"network-{len(self.items) + 1:04d}",
        )
        self.items.append(network)
        self.client.created.append(network)
        return network


class _FakeDockerImages(_FakeDockerCollection):
    def __init__(self, client: "FakeDockerClient") -> None:
        super().__init__(client, "images")

    def get(self, identifier: str) -> FakeDockerImage:
        self.client._raise_if_failed("images.get")
        for image in self._active():
            if identifier in {image.name, image.id}:
                return image  # type: ignore[return-value]
        raise ImageNotFound(f"image '{identifier}' was not found")

    def pull(self, repository: str, **kwargs: Any) -> FakeDockerImage:
        self.client._raise_if_failed("images.pull")
        image = FakeDockerImage(
            self.client,
            name=repository,
            labels=kwargs.get("labels"),
            resource_id=f"image-{len(self.items) + 1:04d}",
        )
        self.items.append(image)
        self.client.created.append(image)
        return image

    def build(self, **kwargs: Any) -> tuple[FakeDockerImage, list[dict[str, str]]]:
        self.client._raise_if_failed("images.build")
        image = self.pull(
            kwargs.get("tag", f"image-{len(self.items) + 1:04d}"),
            labels=kwargs.get("labels"),
        )
        return image, []

    def remove(self, image: str, **kwargs: Any) -> None:
        self.client._raise_if_failed("images.remove")
        self.get(image).remove(**kwargs)


class _FakeDockerAPI:
    def __init__(self, client: "FakeDockerClient") -> None:
        self.client = client
        self.exec_calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def exec_create(self, container_id: str, command: list[str], **kwargs: Any) -> dict[str, str]:
        self.client._raise_if_failed("api.exec_create")
        self.exec_calls.append((container_id, command, kwargs))
        return {"Id": f"exec-{len(self.exec_calls):04d}"}

    def exec_start(self, exec_id: str, **_: Any) -> bytes:
        self.client._raise_if_failed("api.exec_start")
        return b""

    def exec_resize(self, exec_id: str, **_: Any) -> None:
        self.client._raise_if_failed("api.exec_resize")

    def exec_inspect(self, exec_id: str) -> dict[str, int]:
        self.client._raise_if_failed("api.exec_inspect")
        return {"ExitCode": 0}

    def attach_socket(self, container: str, **_: Any) -> Any:
        self.client._raise_if_failed("api.attach_socket")
        return None

    def df(self) -> dict[str, list[Any]]:
        self.client._raise_if_failed("api.df")
        return {"Volumes": []}


class FakeDockerClient:
    """Models Docker state for tests that need lifecycle decisions without Docker.

    Services discover owned resources through labels and clean them up later. Keeping that
    state in one fake lets tests assert both actions without recreating SDK-shaped stubs.
    """

    def __init__(self) -> None:
        self.created: list[FakeDockerResource] = []
        self.removed: list[FakeDockerResource] = []
        self._failures: dict[str, deque[Exception]] = defaultdict(deque)
        self.containers = _FakeDockerContainers(self)
        self.volumes = _FakeDockerVolumes(self)
        self.networks = _FakeDockerNetworks(self)
        self.images = _FakeDockerImages(self)
        self.api = _FakeDockerAPI(self)

    def inject_failure(self, operation: str, error: Exception) -> None:
        """Makes the next named SDK operation raise a real Docker exception instance.

        Operation names follow the SDK path, such as ``volumes.create`` or
        ``container.remove``. A queue makes a test able to model a single partial failure.
        """
        self._failures[operation].append(error)

    def _raise_if_failed(self, operation: str) -> None:
        if failures := self._failures[operation]:
            raise failures.popleft()

    def close(self) -> None:
        self._raise_if_failed("client.close")


@pytest.fixture
def fake_docker_client() -> FakeDockerClient:
    """Provides the shared Docker double for tests that only call services directly."""
    return FakeDockerClient()


@pytest.fixture
def override_docker_client(
    fake_docker_client: FakeDockerClient,
) -> Iterator[FakeDockerClient]:
    """Installs the shared fake through FastAPI's Docker dependency for router tests."""
    from app.docker_client import get_docker_client
    from app.main import app

    def override() -> Iterator[FakeDockerClient]:
        yield fake_docker_client

    missing = object()
    previous = app.dependency_overrides.get(get_docker_client, missing)
    app.dependency_overrides[get_docker_client] = override
    try:
        yield fake_docker_client
    finally:
        if previous is missing:
            app.dependency_overrides.pop(get_docker_client, None)
        else:
            app.dependency_overrides[get_docker_client] = previous


def register_ready_v1_sandbox(
    store: ControllerStore,
    *,
    sandbox_id: str,
    project_id: str,
    project_name: str,
    volume_name: str,
    created_at: str = "",
    remote_url: str | None = None,
    default_branch: str = "main",
    mirror_volume: str | None = None,
    feature_key: str = "test-sandbox",
    feature_title: str | None = None,
    desired_state: str = "active",
    lifecycle_status: SandboxLifecycleStatus = SandboxLifecycleStatus.READY,
    base_ref: str | None = None,
    created_base_commit: str | None = None,
    current_base_commit: str | None = None,
    feature_branch: str | None = None,
    db_engine: str | None = None,
    db_name: str | None = None,
    db_data_volume: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Register a remote project and a managed sandbox in a lifecycle state.

    V1 sandboxes require a remote project, a manifest, and a valid lifecycle
    transition. Defaults model a ready sandbox while optional manifest fields
    let focused tests set up the state their code path reads.
    """
    store.register_v1_project(
        project_id=project_id,
        remote_url=remote_url or f"https://example.test/{project_id}.git",
        default_branch=default_branch,
        mirror_volume=mirror_volume or f"prj-{project_id[:12]}-mirror",
        created_at=created_at,
    )
    registration = store.register_v1_sandbox(
        sandbox_id=sandbox_id,
        project_id=project_id,
        project_name=project_name,
        volume_name=volume_name,
        created_at=created_at,
    )
    manifest = SandboxManifest(
        sandbox_id=sandbox_id,
        lifecycle_version="v1",
        feature_key=feature_key,
        feature_title=feature_title,
        desired_state=desired_state,
        lifecycle_status=lifecycle_status,
        base_ref=base_ref,
        created_base_commit=created_base_commit,
        current_base_commit=current_base_commit,
        feature_branch=feature_branch,
        db_engine=db_engine,
        db_name=db_name,
        db_data_volume=db_data_volume,
    )
    if not registration[1]:
        return registration
    if lifecycle_status is SandboxLifecycleStatus.CREATING:
        write_manifest(store, manifest)
    else:
        assert transition_sandbox_lifecycle(
            store,
            manifest,
            to_status=lifecycle_status,
        )
    return registration


@pytest.fixture(autouse=True)
def _isolated_controller_database(tmp_path, monkeypatch):
    """Points every test's controller store at a throwaway tmp_path.

    Without this, any test that exercises a router endpoint resolves the
    get_controller_store dependency and writes to the live database at
    backend/.controller-data/controller.sqlite3. get_controller_settings is
    lru_cache'd, so setting the env var alone is not enough: the cache must
    be cleared after the env var changes, and again on teardown, or a stale
    ControllerSettings leaks into the next test. The environment variable is
    set unconditionally: honouring an ambient CONTROLLER_DATA_DIRECTORY would
    silently point the suite at whatever real directory a shell exported. A
    test that needs its own location still wins, because its own setenv runs
    after this fixture.
    """
    monkeypatch.setenv("CONTROLLER_DATA_DIRECTORY", str(tmp_path / "controller-data"))
    get_controller_settings.cache_clear()
    yield
    get_controller_settings.cache_clear()
