from types import SimpleNamespace

import pytest

from app.previews.models import (
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewEnvironmentSource,
    PreviewMode,
    PreviewRuntime,
    PreviewServiceType,
    PreviewSharing,
)
from app.previews.service import PreviewOperationError, _validate_sharing
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

    def run(self, **kwargs: object) -> bytes:
        self.calls.append(kwargs)
        command = " ".join(kwargs.get("command", []))
        if "/credentials/postgres.json" in command:
            return b'{"username":"sandbox_role","password":"app-password","root_password":"root-password"}'
        return b""


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
    run = fake_docker_client.containers.run

    def capture(**kwargs: object):
        calls.append(kwargs)
        return run(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "run", capture)
    provision = SQLITE_DATABASE.provision(request)

    assert provision is not None
    assert provision.container is None
    assert provision.credentials == {}
    assert fake_docker_client.volumes.get("sbx-aaaaaaaaaaaa-db")
    assert fake_docker_client.networks.items == []
    helper = fake_docker_client.containers.items[-1]
    assert helper.removed is True
    assert helper.attrs["Config"]["Image"] == "alpine:3.21"
    assert calls[-1]["network_disabled"] is True
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
