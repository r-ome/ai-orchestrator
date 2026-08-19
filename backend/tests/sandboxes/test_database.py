import base64
import json
import threading
from collections.abc import Callable
from types import SimpleNamespace

import pytest

import app.previews.service as preview_service
import app.previews.sharing as preview_sharing
import app.sandboxes.database as sandbox_database
from app.previews.config import get_preview_settings
from app.previews.errors import PreviewOperationError
from app.previews.models import (
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewEnvironmentSource,
    PreviewMode,
    PreviewRuntime,
    PreviewServiceType,
    PreviewSharing,
)
from app.previews.sharing import _validate_sharing
from app.sandboxes.database import (
    MYSQL_DATABASE,
    POSTGRESQL_DATABASE,
    SQLITE_DATABASE,
    DatabaseConnectionRequest,
    DatabaseDropRequest,
    DatabaseEngine,
    DatabaseProvisionRequest,
    DatabaseSchemaProvisionRequest,
    SQLITE_DATABASE_PATH,
    postgres_drop_statements,
    postgres_provision_statements,
    postgres_shared_database_names,
    sqlite_data_volume,
    schema_baseline_hash,
)
from conftest import FakeDockerClient


def test_mysql_engine_implements_the_small_database_protocol() -> None:
    assert isinstance(MYSQL_DATABASE, DatabaseEngine)
    assert MYSQL_DATABASE.supports_template is False


def test_mysql_engine_builds_the_existing_database_url() -> None:
    config = PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        runtime=PreviewRuntime.NEXTJS,
        image="node:22-bookworm-slim",
        start_command="npm run start",
        container_port=3000,
        services={
            "database": PreviewDependencyService(
                type=PreviewServiceType.MYSQL,
                image="mysql:8.4",
            )
        },
        environment={
            "DATABASE_URL": PreviewEnvironmentSource(from_service="database")
        },
    )

    environment = MYSQL_DATABASE.connection_url(
        DatabaseConnectionRequest(
            config=config,
            database=config.services["database"],
            credentials={"username": "preview user", "password": "pa/ss"},
            error=RuntimeError,
        )
    )

    assert environment == {
        "DATABASE_URL": "mysql://preview%20user:pa%2Fss@database:3306/atc_preview"
    }


def _config(service_type: PreviewServiceType) -> PreviewConfiguration:
    return PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        runtime=PreviewRuntime.NEXTJS,
        image="node:22-bookworm-slim",
        start_command="npm run start",
        container_port=3000,
        services={
            "database": PreviewDependencyService(type=service_type, image="database:latest")
        },
        environment={"DATABASE_URL": PreviewEnvironmentSource(from_service="database")},
    )


class _PostgresDocker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.containers = self

    def create(self, **kwargs: object) -> "_PostgresContainer":
        self.calls.append(kwargs)
        return _PostgresContainer(kwargs)


class _PostgresContainer:
    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments

    def start(self) -> None:
        pass

    def wait(self, *, timeout: int) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        command = " ".join(self.arguments.get("command", []))
        if "/credentials/postgres.json" in command:
            return b'{"username":"sandbox_role","password":"app-password","root_password":"root-password"}'
        return b""

    def remove(self, *, force: bool) -> None:
        pass


class _CredentialVolumeDocker(FakeDockerClient):
    """Adds the credential-file semantics that the shared Docker fake omits."""

    def __init__(
        self,
        empty_read_barrier: threading.Barrier,
        *,
        barrier_timeout: float,
    ) -> None:
        super().__init__()
        self.empty_read_barrier = empty_read_barrier
        self.barrier_timeout = barrier_timeout
        self.credential_files: dict[str, dict[str, bytes]] = {}
        self.write_payloads: list[bytes] = []
        self.write_scripts: list[str] = []
        self.maximum_concurrent_empty_reads = 0
        self._active_empty_reads = 0
        self._credential_lock = threading.Lock()

    def run_database_command(
        self,
        docker_client: object,
        *,
        image: str,
        command: list[str],
        environment: dict[str, str] | None = None,
        volumes: dict[str, object] | None = None,
        network: str | None = None,
        tmpfs_size: str = "256m",
    ) -> str:
        del image
        del network
        del tmpfs_size
        assert docker_client is self
        assert volumes is not None
        volume_name = next(iter(volumes))
        environment = environment or {}
        script = command[-1]

        if "DATABASE_CREDENTIALS" in environment:
            filename = environment["DATABASE_CREDENTIAL_FILE"]
            payload = base64.b64decode(environment["DATABASE_CREDENTIALS"])
            with self._credential_lock:
                self.write_payloads.append(payload)
                self.write_scripts.append(script)
                files = self.credential_files.setdefault(volume_name, {})
                if 'ln "$temporary" "$destination"' in script:
                    files.setdefault(filename, payload)
                else:
                    files[filename] = payload
            return ""

        filename = next(
            name
            for name in ("mysql.json", "postgres.json")
            if f"/credentials/{name}" in script
        )
        with self._credential_lock:
            contents = self.credential_files.get(volume_name, {}).get(filename)
            if contents is not None:
                return contents.decode("utf-8")
            self._active_empty_reads += 1
            self.maximum_concurrent_empty_reads = max(
                self.maximum_concurrent_empty_reads,
                self._active_empty_reads,
            )
        try:
            self.empty_read_barrier.wait(timeout=self.barrier_timeout)
        except threading.BrokenBarrierError:
            pass
        finally:
            with self._credential_lock:
                self._active_empty_reads -= 1
        return ""


def _thread_results(
    *calls: Callable[[], object],
) -> tuple[list[object], list[BaseException]]:
    results: list[object] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def run(call: Callable[[], object]) -> None:
        try:
            result = call()
        except BaseException as error:
            with result_lock:
                errors.append(error)
        else:
            with result_lock:
                results.append(result)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    return results, errors


def test_server_credentials_use_one_atomic_file_across_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _CredentialVolumeDocker(
        threading.Barrier(2),
        barrier_timeout=5,
    )
    credentials_volume = docker.volumes.create(name="project-credentials")
    monkeypatch.setattr(
        sandbox_database,
        "_run_database_command",
        docker.run_database_command,
    )

    def read_or_create() -> dict[str, str]:
        return sandbox_database._read_or_create_server_credentials(
            docker,
            "postgres:17",
            credentials_volume,
            filename="postgres.json",
            error=sandbox_database.SandboxDatabaseError,
        )

    results, errors = _thread_results(read_or_create, read_or_create)

    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    files = docker.credential_files[credentials_volume.name]
    assert list(files) == ["postgres.json"]
    assert json.loads(files["postgres.json"]) == results[0]
    assert len(docker.write_payloads) == 2
    assert len(set(docker.write_payloads)) == 2
    assert all("umask 077" in script for script in docker.write_scripts)
    assert all("mktemp /credentials/" in script for script in docker.write_scripts)
    assert all(
        'ln "$temporary" "$destination"' in script
        for script in docker.write_scripts
    )


def test_shared_server_locks_allow_different_projects_to_provision_in_parallel() -> None:
    barrier = threading.Barrier(2)

    def provision(server_name: str) -> None:
        with sandbox_database.shared_database_server_lock(server_name):
            barrier.wait(timeout=5)

    _, errors = _thread_results(
        lambda: provision("project-one-mysql"),
        lambda: provision("project-two-mysql"),
    )

    assert errors == []


def test_sandbox_and_preview_shared_server_creation_use_the_same_project_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name_barrier = threading.Barrier(2)
    docker = _CredentialVolumeDocker(
        threading.Barrier(2),
        barrier_timeout=1,
    )
    names = sandbox_database.mysql_shared_database_names("project-123456789")

    def sandbox_names(project_key: str, engine_name: str) -> dict[str, str]:
        assert project_key == "project-123456789"
        assert engine_name == "mysql"
        name_barrier.wait(timeout=5)
        return names

    def preview_names(project_key: str) -> dict[str, str]:
        assert project_key == "project-123456789"
        name_barrier.wait(timeout=5)
        return names

    monkeypatch.setattr(sandbox_database, "shared_database_names", sandbox_names)
    monkeypatch.setattr(preview_sharing, "_shared_database_names", preview_names)
    monkeypatch.setattr(sandbox_database, "ensure_image", lambda *_: None)
    monkeypatch.setattr(preview_sharing, "_ensure_preview_image", lambda *_: None)
    monkeypatch.setattr(
        sandbox_database,
        "_wait_for_server_health",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        preview_sharing,
        "_wait_for_mysql_health",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        sandbox_database,
        "_run_database_command",
        docker.run_database_command,
    )
    settings = get_preview_settings()
    database = PreviewDependencyService(
        type=PreviewServiceType.MYSQL,
        image="mysql:8.4",
    )

    def from_sandbox() -> object:
        return sandbox_database._ensure_shared_server(
            docker,
            settings,
            project_id="project-123456789",
            project_source="/projects/sample",
            engine_name="mysql",
        )

    def from_preview() -> object:
        return preview_sharing._shared_database_server(
            docker,
            settings,
            project_key="project-123456789",
            source_path="/projects/sample",
            database=database,
            report=lambda *_: None,
        )

    results, errors = _thread_results(from_sandbox, from_preview)

    assert errors == []
    assert len(results) == 2
    sandbox_result = next(result for result in results if hasattr(result, "container"))
    preview_result = next(result for result in results if isinstance(result, tuple))
    assert sandbox_result.container is preview_result[0]  # type: ignore[union-attr, index]
    servers = [
        container
        for container in docker.containers.items
        if container.name == names["container"]
    ]
    assert len(servers) == 1
    assert docker.maximum_concurrent_empty_reads == 1
    assert len(docker.write_payloads) == 1
    assert list(docker.credential_files[names["credentials"]]) == ["mysql.json"]


def test_sandbox_shared_server_refuses_an_existing_container_with_another_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDockerClient()
    project_id = "project-123456789"
    names = sandbox_database.mysql_shared_database_names(project_id)
    docker.containers.create(
        name=names["container"],
        image="mysql:8.0",
        labels={"orchestrator.shared-database.image": "mysql:8.0"},
    )
    monkeypatch.setattr(sandbox_database, "ensure_image", lambda *_: None)

    def unexpected_engine(*_: object) -> object:
        raise AssertionError("image mismatch must fail before provisioning")

    monkeypatch.setattr(sandbox_database, "database_engine", unexpected_engine)

    with pytest.raises(sandbox_database.SandboxDatabaseError) as error:
        sandbox_database._ensure_shared_server(
            docker,
            get_preview_settings(),
            project_id=project_id,
            project_source="/projects/sample",
            engine_name="mysql",
        )

    assert error.value.status_code == 409
    assert error.value.detail == (
        "This project's shared database runs mysql:8.0; "
        "the sandbox asks for mysql:8.4"
    )


def test_postgres_engine_uses_a_root_only_client_on_the_shared_network() -> None:
    assert isinstance(POSTGRESQL_DATABASE, DatabaseEngine)
    assert POSTGRESQL_DATABASE.supports_template is True
    docker = _PostgresDocker()
    credentials = SimpleNamespace(name="postgres-credentials")
    sandbox_id = "a" * 32
    statements = postgres_provision_statements(sandbox_id, "app-password", RuntimeError)

    POSTGRESQL_DATABASE.provision(
        DatabaseSchemaProvisionRequest(
            docker_client=docker,  # type: ignore[arg-type]
            image="postgres:17",
            network_name="project-postgres-net",
            host="project-postgres",
            credentials_volume=credentials,
            statements=statements,
            error=RuntimeError,
        )
    )

    admin_call = docker.calls[-1]
    assert admin_call["network"] == "project-postgres-net"
    assert "psql" in " ".join(admin_call["command"])  # type: ignore[arg-type]
    assert admin_call["environment"] == {
        "PREVIEW_SQL": 'CREATE ROLE "sbx_aaaaaaaaaaaaaaaa" LOGIN PASSWORD \'app-password\';\n'
        'CREATE DATABASE "sbx_aaaaaaaaaaaaaaaa" OWNER "sbx_aaaaaaaaaaaaaaaa";',
        "PREVIEW_HOST": "project-postgres",
        "PGPASSWORD": "root-password",
    }
    assert "root-password" not in admin_call["environment"]["PREVIEW_SQL"]  # type: ignore[index]

    POSTGRESQL_DATABASE.drop(
        DatabaseDropRequest(
            docker_client=docker,  # type: ignore[arg-type]
            image="postgres:17",
            network_name="project-postgres-net",
            host="project-postgres",
            credentials_volume=credentials,
            statements=postgres_drop_statements(sandbox_id, RuntimeError),
            error=RuntimeError,
        )
    )
    assert "pg_terminate_backend" in docker.calls[-1]["environment"]["PREVIEW_SQL"]  # type: ignore[index]
    assert 'DROP DATABASE IF EXISTS "sbx_aaaaaaaaaaaaaaaa"' in docker.calls[-1]["environment"]["PREVIEW_SQL"]  # type: ignore[index]
    assert 'DROP ROLE IF EXISTS "sbx_aaaaaaaaaaaaaaaa"' in docker.calls[-1]["environment"]["PREVIEW_SQL"]  # type: ignore[index]


def test_postgres_engine_builds_a_postgres_connection_url() -> None:
    config = _config(PreviewServiceType.POSTGRES)

    assert POSTGRESQL_DATABASE.connection_url(
        DatabaseConnectionRequest(
            config=config,
            database=config.services["database"],
            credentials={"username": "preview user", "password": "pa/ss"},
            error=RuntimeError,
        )
    ) == {"DATABASE_URL": "postgres://preview%20user:pa%2Fss@database:5432/atc_preview"}


def test_sqlite_uses_a_sandbox_volume_without_a_server_network_or_credentials(
    fake_docker_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert isinstance(SQLITE_DATABASE, DatabaseEngine)
    assert SQLITE_DATABASE.supports_template is True
    assert sqlite_data_volume("a" * 32) == "sbx-aaaaaaaaaaaa-db"
    request = DatabaseProvisionRequest(
        docker_client=fake_docker_client,
        image="ignored-by-sqlite",
        database="ignored",
        container_name="must-not-exist",
        labels={"orchestrator.sandbox.id": "sandbox"},
        data_volume="sbx-aaaaaaaaaaaa-db",
        credentials_volume=SimpleNamespace(name="must-not-exist"),
        network_name="must-not-exist",
        memory_limit="1g",
        nano_cpus=1,
        pids_limit=1,
        error=RuntimeError,
    )

    calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    provision = SQLITE_DATABASE.provision(request)

    assert provision is not None
    assert provision.container is None
    assert provision.credentials == {}
    assert fake_docker_client.volumes.get("sbx-aaaaaaaaaaaa-db")
    assert fake_docker_client.networks.items == []
    helper = fake_docker_client.containers.items[-1]
    assert helper.removed is True
    assert helper.attrs["Config"]["Image"] == "alpine:3.21"
    assert calls[-1]["network_mode"] == "none"
    assert calls[-1]["volumes"] == {"sbx-aaaaaaaaaaaa-db": {"bind": "/database", "mode": "rw"}}

    SQLITE_DATABASE.drop(
        DatabaseDropRequest(
            docker_client=fake_docker_client,
            image="ignored-by-sqlite",
            network_name="must-not-exist",
            host="must-not-exist",
            credentials_volume=SimpleNamespace(name="must-not-exist"),
            statements=[],
            data_volume="sbx-aaaaaaaaaaaa-db",
            error=RuntimeError,
        )
    )
    assert "rm -f /database/database.sqlite3" in str(calls[-1]["command"])

    config = _config(PreviewServiceType.SQLITE)
    assert SQLITE_DATABASE.connection_url(
        DatabaseConnectionRequest(
            config=config,
            database=config.services["database"],
            credentials={},
            error=RuntimeError,
        )
    ) == {"DATABASE_URL": f"file:{SQLITE_DATABASE_PATH}"}


def test_shared_data_remains_mysql_only() -> None:
    mysql = PreviewDependencyService(
        type=PreviewServiceType.MYSQL,
        image="mysql:8.4",
        sharing="shared_data",
        share_target="a" * 32,
    )
    assert mysql.sharing.value == "shared_data"

    with pytest.raises(ValueError, match="only for MySQL"):
        PreviewDependencyService(
            type=PreviewServiceType.POSTGRES,
            image="postgres:17",
            sharing="shared_data",
            share_target="a" * 32,
        )


def test_shared_data_is_refused() -> None:
    config = _config(PreviewServiceType.MYSQL)
    config.services["database"].sharing = PreviewSharing.SHARED_DATA
    config.services["database"].share_target = "a" * 32

    with pytest.raises(PreviewOperationError, match="shared_data is unavailable"):
        _validate_sharing(config)
    with pytest.raises(ValueError, match="only for MySQL"):
        PreviewDependencyService(
            type=PreviewServiceType.SQLITE,
            image="ignored",
            sharing="shared_data",
            share_target="a" * 32,
        )


def test_postgres_shared_names_are_engine_keyed() -> None:
    assert postgres_shared_database_names("1f2e3d4c5b6a7988") == {
        "container": "orchestrator-shared-db-1f2e3d4c5b6a-postgres",
        "data": "orchestrator-shared-db-1f2e3d4c5b6a-postgres-data",
        "credentials": "orchestrator-shared-db-1f2e3d4c5b6a-postgres-credentials",
        "network": "orchestrator-shared-db-1f2e3d4c5b6a-postgres-net",
    }


def test_schema_baseline_hash_is_stable_over_sorted_path_and_bytes() -> None:
    first = schema_baseline_hash(
        {"migrations/002.sql": b"two", "migrations/001.sql": b"one"}
    )
    second = schema_baseline_hash(
        {"migrations/001.sql": b"one", "migrations/002.sql": b"two"}
    )

    assert first == second
    assert first != schema_baseline_hash(
        {"migrations/001.sql": b"changed", "migrations/002.sql": b"two"}
    )
