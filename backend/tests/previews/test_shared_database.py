from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from conftest import register_ready_v1_sandbox
from docker.errors import NotFound
from pydantic import ValidationError

from app.controller.store import ControllerStore
from app.previews.detection import proposal_digest
from app.previews.errors import PreviewOperationError
from app.previews.models import (
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewEnvironmentSource,
    PreviewMode,
    PreviewPersistence,
    PreviewRuntime,
    PreviewServiceType,
    PreviewSharing,
)
from app.previews.sharing import (
    _attach_shared_database,
    _identifier,
    _release_shared_database,
    _shared_database_names,
    _shared_schema_name,
    _shared_server_is_idle,
    _sharing_state,
    _validate_sharing,
)

PROJECT_KEY = "1f2e3d4c5b6a7988"
OWNER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
GUEST = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTHER = "cccccccccccccccccccccccccccccccc"


def _store(tmp_path: Path) -> ControllerStore:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    for sandbox_id, name in (
        (OWNER, "sample-sandbox-1"),
        (GUEST, "sample-sandbox-2"),
        (OTHER, "sample-sandbox-3"),
    ):
        register_ready_v1_sandbox(
            store,
            sandbox_id=sandbox_id,
            project_id=PROJECT_KEY,
            project_name=name,
            volume_name=f"orchestrator-project-{name}",
            created_at="2026-08-04T00:00:00Z",
            feature_key=f"test-{sandbox_id[:12]}",
        )
    return store


def _own_schema(
    store: ControllerStore, sandbox_id: str, image: str = "mysql:8.4"
) -> None:
    store.record_shared_schema(
        sandbox_id=sandbox_id,
        project_id=PROJECT_KEY,
        owner_sandbox_id=sandbox_id,
        sharing=PreviewSharing.SHARED_SERVER.value,
        schema_name=_shared_schema_name(sandbox_id),
        user_name=_shared_schema_name(sandbox_id),
        image=image,
        persistence=PreviewPersistence.EPHEMERAL.value,
    )


def _guest_schema(store: ControllerStore, sandbox_id: str, owner: str) -> None:
    store.record_shared_schema(
        sandbox_id=sandbox_id,
        project_id=PROJECT_KEY,
        owner_sandbox_id=owner,
        sharing=PreviewSharing.SHARED_DATA.value,
        schema_name=_shared_schema_name(owner),
        user_name=_shared_schema_name(sandbox_id),
        image="mysql:8.4",
        persistence=PreviewPersistence.EPHEMERAL.value,
    )


class _FakeDocker:
    """A Docker client with a healthy shared server that accepts SQL."""

    class _Container:
        name = "orchestrator-shared-db-1f2e3d4c5b6a"
        status = "running"
        attrs: ClassVar[dict[str, object]] = {}

        def reload(self) -> None:
            return None

        def remove(self, force: bool = False, v: bool = False) -> None:
            del force
            del v

    class _Containers:
        def __init__(self, outer: "_FakeDocker") -> None:
            self.outer = outer

        def get(self, name: str) -> object:
            del name
            return self.outer.container

        def create(self, **kwargs: object) -> "_FakeDocker._RunContainer":
            return _FakeDocker._RunContainer(self.outer, kwargs)

    class _RunContainer:
        def __init__(self, outer: "_FakeDocker", arguments: dict[str, object]) -> None:
            self.outer = outer
            self.arguments = arguments

        def start(self) -> None:
            return None

        def wait(self, *, timeout: int) -> dict[str, int]:
            del timeout
            return {"StatusCode": 0}

        def logs(self, *, stdout: bool, stderr: bool) -> bytes:
            del stderr
            command = self.arguments.get("command")
            text = " ".join(command) if isinstance(command, list) else str(command)
            if "/credentials/mysql.json" in text:
                return b'{"username":"root","password":"p","root_password":"secret"}'
            if stdout:
                self.outer.statements.append(
                    str(
                        (self.arguments.get("environment") or {}).get("PREVIEW_SQL", "")
                    )
                )
            return b""

        def remove(self, *, force: bool) -> None:
            del force

    class _Volumes:
        name = "orchestrator-shared-db-1f2e3d4c5b6a-credentials"

        def get(self, name: str) -> object:
            self.name = name
            return self

    class _Networks:
        def list(self, names: list[str] | None = None) -> list[object]:
            del names
            return []

    def __init__(self) -> None:
        self.container = self._Container()
        self.statements: list[str] = []
        self.containers = self._Containers(self)
        self.volumes = self._Volumes()
        self.networks = self._Networks()


class _MissingDocker:
    """A Docker client whose shared server and volumes are already gone.

    Release must still settle the controller's own records, because a stop can
    follow a daemon restart that took the containers with it.
    """

    class _Containers:
        def get(self, name: str) -> object:
            raise NotFound(name)

    class _Volumes:
        def get(self, name: str) -> object:
            raise NotFound(name)

    class _Networks:
        def list(self, names: list[str] | None = None) -> list[object]:
            del names
            return []

    def __init__(self) -> None:
        self.containers = self._Containers()
        self.volumes = self._Volumes()
        self.networks = self._Networks()


def _config(
    sharing: PreviewSharing = PreviewSharing.ISOLATED,
    share_target: str = "",
    image: str = "mysql:8.4",
) -> PreviewConfiguration:
    return PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        runtime=PreviewRuntime.NEXTJS,
        image="node:22-bookworm-slim",
        start_command="npm run start",
        container_port=3000,
        services={
            "database": PreviewDependencyService(
                type=PreviewServiceType.MYSQL,
                image=image,
                sharing=sharing,
                share_target=share_target,
            )
        },
        environment={"DATABASE_URL": PreviewEnvironmentSource(from_service="database")},
    )


def test_sharing_defaults_to_isolated_for_configurations_without_the_field() -> None:
    service = PreviewDependencyService(type=PreviewServiceType.MYSQL, image="mysql:8.4")

    assert service.sharing is PreviewSharing.ISOLATED
    assert service.share_target == ""


def test_shared_data_requires_a_target() -> None:
    with pytest.raises(ValidationError):
        PreviewDependencyService(
            type=PreviewServiceType.MYSQL,
            image="mysql:8.4",
            sharing=PreviewSharing.SHARED_DATA,
        )


def test_a_target_without_shared_data_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PreviewDependencyService(
            type=PreviewServiceType.MYSQL,
            image="mysql:8.4",
            sharing=PreviewSharing.SHARED_SERVER,
            share_target=OWNER,
        )


def test_changing_sharing_changes_the_proposal_digest() -> None:
    """Sharing sits in the config, so a change forces a fresh approval."""
    files = {"package.json": "hash"}

    isolated = proposal_digest(_config(), files)
    shared = proposal_digest(_config(PreviewSharing.SHARED_SERVER), files)
    joined = proposal_digest(_config(PreviewSharing.SHARED_DATA, OWNER), files)

    assert len({isolated, shared, joined}) == 3


def test_sharing_state_names_both_sides_of_the_coupling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _own_schema(store, OWNER)
    _guest_schema(store, GUEST, OWNER)

    owner_state = _sharing_state(store, OWNER)
    guest_state = _sharing_state(store, GUEST)

    assert owner_state is not None
    assert owner_state.attached_project_names == ["sample-sandbox-2"]
    assert guest_state is not None
    assert guest_state.sharing is PreviewSharing.SHARED_DATA
    assert guest_state.owner_project_name == "sample-sandbox-1"
    assert guest_state.schema_name == owner_state.schema_name


def test_attaching_refuses_a_guest_from_an_older_approval(tmp_path: Path) -> None:
    """`_validate_sharing` runs at approval, so a stored approval can predate it.

    Docker and the settings are None on purpose: the refusal must come before
    any provisioning, so nothing is created for a guest that cannot start.
    """
    store = _store(tmp_path)
    database = _config(PreviewSharing.SHARED_DATA, OWNER).services["database"]

    with pytest.raises(PreviewOperationError) as error:
        _attach_shared_database(
            cast(Any, None),
            store,
            cast(Any, None),
            sandbox_id=GUEST,
            project_key=PROJECT_KEY,
            source_path="/projects/sample",
            database=database,
            run_network=None,
            report=lambda *_: None,
        )

    assert error.value.status_code == 422
    assert "shared_data is unavailable" in error.value.detail


def test_validate_sharing_refuses_shared_data(tmp_path: Path) -> None:
    """Every sandbox owns its schema, so no sandbox can join another's data."""
    store = _store(tmp_path)
    _own_schema(store, OWNER)

    with pytest.raises(PreviewOperationError) as error:
        _validate_sharing(_config(PreviewSharing.SHARED_DATA, OWNER))

    assert error.value.status_code == 422
    assert "shared_data is unavailable" in error.value.detail


def test_validate_sharing_ignores_isolated_configurations(tmp_path: Path) -> None:
    _validate_sharing(_config())


def test_a_guest_release_drops_its_user_and_never_the_owner_schema(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _own_schema(store, OWNER)
    _guest_schema(store, GUEST, OWNER)
    docker = _FakeDocker()

    outcome = _release_shared_database(docker, store, sandbox_id=GUEST)

    executed = "\n".join(docker.statements)
    assert outcome["dropped_schema"] is False
    assert "DROP USER" in executed
    assert "DROP DATABASE" not in executed
    assert store.shared_schema(GUEST) is None
    assert store.shared_schema(OWNER) is not None


def test_an_owner_keeps_its_schema_while_a_guest_is_attached(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _own_schema(store, OWNER)
    _guest_schema(store, GUEST, OWNER)
    docker = _FakeDocker()

    outcome = _release_shared_database(docker, store, sandbox_id=OWNER)

    assert outcome["dropped_schema"] is False
    assert outcome["kept_for_attached_sandboxes"] == 1
    assert "DROP DATABASE" not in "\n".join(docker.statements)
    assert store.shared_schema(OWNER) is not None


def test_an_ephemeral_owner_drops_its_schema_once_alone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _own_schema(store, OWNER)
    docker = _FakeDocker()

    outcome = _release_shared_database(docker, store, sandbox_id=OWNER)

    assert outcome["dropped_schema"] is True
    assert f"DROP DATABASE IF EXISTS `{_shared_schema_name(OWNER)}`" in "\n".join(
        docker.statements
    )
    assert store.shared_schema(OWNER) is None


def test_a_persistent_owner_keeps_its_record_after_stopping(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_shared_schema(
        sandbox_id=OWNER,
        project_id=PROJECT_KEY,
        owner_sandbox_id=OWNER,
        sharing=PreviewSharing.SHARED_SERVER.value,
        schema_name=_shared_schema_name(OWNER),
        user_name=_shared_schema_name(OWNER),
        image="mysql:8.4",
        persistence=PreviewPersistence.PERSISTENT.value,
    )

    outcome = _release_shared_database(_FakeDocker(), store, sandbox_id=OWNER)

    assert outcome["dropped_schema"] is False
    assert outcome["kept_record"] is True
    assert store.shared_schema(OWNER) is not None


def test_an_unreachable_server_keeps_the_record_for_later_cleanup(
    tmp_path: Path,
) -> None:
    """A schema that could not be dropped is still there, so the record stays."""
    store = _store(tmp_path)
    _own_schema(store, OWNER)

    outcome = _release_shared_database(_MissingDocker(), store, sandbox_id=OWNER)

    assert outcome["pending_cleanup"] is True
    assert store.shared_schema(OWNER) is not None


def test_releasing_a_sandbox_without_a_shared_database_does_nothing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    assert _release_shared_database(
        _MissingDocker(),
        store,
        sandbox_id=OWNER,
    ) == {"released": False}


def test_idleness_ignores_records_and_follows_active_previews(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _own_schema(store, OWNER)

    assert _shared_server_is_idle(store, PROJECT_KEY) is True

    store.create_preview_run(
        {
            "id": "run-1",
            "sandbox_id": OWNER,
            "proposal_id": "proposal-1",
            "mode": "native",
            "status": "running",
            "selected_service": "app",
            "container_port": 3000,
            "host_port": None,
            "config_json": "{}",
            "config_digest": "digest",
            "network_name": "",
            "created_at": "2026-08-04T00:00:00Z",
            "started_at": "2026-08-04T00:00:00Z",
            "expires_at": None,
            "last_activity_at": "2026-08-04T00:00:00Z",
        }
    )

    assert _shared_server_is_idle(store, PROJECT_KEY) is False


def test_shared_names_carry_the_engine() -> None:
    names = _shared_database_names(PROJECT_KEY)

    assert names["container"] == "orchestrator-shared-db-1f2e3d4c5b6a-mysql"
    assert names["data"] == "orchestrator-shared-db-1f2e3d4c5b6a-mysql-data"
    assert (
        names["credentials"] == "orchestrator-shared-db-1f2e3d4c5b6a-mysql-credentials"
    )
    assert names["network"] == "orchestrator-shared-db-1f2e3d4c5b6a-mysql-net"


def test_schema_identifiers_stay_within_mysql_limits() -> None:
    schema = _shared_schema_name(OWNER)

    assert schema.startswith("sbx_")
    assert len(schema) <= 32
    assert _identifier("A-B_C!d") == "abcd"
