import pytest

from app.sandboxes.engine_detection import (
    NO_DATABASE,
    detect_engine,
    discover_engine,
    normalize_confirmable_engine,
    normalize_engine,
)


def test_no_files_propose_no_database() -> None:
    detection = detect_engine({})

    assert detection.proposed_engine == NO_DATABASE
    assert detection.signals == ()


def test_tracked_database_path_does_not_propose_no_database() -> None:
    detection = detect_engine({}, tracked_paths=("data.sqlite3",))

    assert detection.proposed_engine is None
    assert detection.tracked_database_paths == ("data.sqlite3",)


def test_only_confirmation_normalizes_no_database() -> None:
    assert normalize_confirmable_engine("none") == NO_DATABASE
    assert normalize_engine("none") is None


@pytest.mark.parametrize(
    ("files", "engine"),
    [
        ({"prisma/schema.prisma": b'datasource db { provider = "mysql" }'}, "mysql"),
        ({".env.example": b"DATABASE_URL=postgresql://localhost/example\n"}, "postgres"),
        ({"project/settings.py": b"'ENGINE': 'django.db.backends.sqlite3'"}, "sqlite"),
        ({"config/database.yml": b"development:\n  adapter: mysql2\n"}, "mysql"),
        ({"alembic.ini": b"sqlalchemy.url = postgresql://localhost/example"}, "postgres"),
        ({"docker-compose.yml": b"services:\n  db:\n    image: mariadb:11\n"}, "mysql"),
        ({"package.json": b'{"dependencies":{"better-sqlite3":"11"}}'}, "sqlite"),
        ({"requirements.txt": b"PyMySQL==1.1\n"}, "mysql"),
    ],
)
def test_detects_each_database_engine_signal(
    files: dict[str, bytes], engine: str
) -> None:
    detection = detect_engine(files)

    assert detection.proposed_engine == engine
    assert [signal.engine for signal in detection.signals] == [engine]


def test_prisma_postgres_ignores_mysql_dependency_when_proposing() -> None:
    detection = detect_engine(
        {
            "prisma/schema.prisma": b'datasource db { provider = "postgresql" }',
            "package.json": b'{"dependencies":{"mysql2":"3"}}',
        }
    )

    assert detection.proposed_engine == "postgres"
    assert [signal.precedence for signal in detection.signals] == [1, 5]
    assert {(signal.engine, signal.source) for signal in detection.signals} == {
        ("postgres", "prisma"),
        ("mysql", "package_dependency"),
    }


def test_prisma_postgres_ignores_mysql_compose_image_when_proposing() -> None:
    detection = detect_engine(
        {
            "prisma/schema.prisma": b'datasource db { provider = "postgresql" }',
            "docker-compose.yml": b"services:\n  db:\n    image: mysql:8\n",
        }
    )

    assert detection.proposed_engine == "postgres"
    assert {(signal.engine, signal.source) for signal in detection.signals} == {
        ("postgres", "prisma"),
        ("mysql", "compose"),
    }


def test_prisma_proposes_project_defined_migration_and_preview_seed_commands() -> None:
    detection = detect_engine(
        {
            "prisma/schema.prisma": b'datasource db { provider = "postgresql" }',
            "package.json": b'{"scripts":{"db:seed:preview":"node seed.js"}}',
        }
    )

    assert detection.migrate_commands == ("npx prisma migrate deploy",)
    assert detection.seed_commands == ("npm run db:seed:preview",)
    assert detection.commands_source == {"migrate": "prisma", "seed": "package_json"}


def test_conflicting_signals_are_all_reported_without_a_proposed_engine() -> None:
    detection = detect_engine(
        {
            "prisma/schema.prisma": b'datasource db { provider = "postgresql" }',
            ".env": b"DATABASE_URL=mysql://localhost/example\n",
        }
    )

    assert detection.proposed_engine is None
    assert detection.ambiguous is True
    assert {(signal.engine, signal.source) for signal in detection.signals} == {
        ("postgres", "prisma"),
        ("mysql", "dotenv"),
    }


def test_mysql_dependency_proposes_mysql_without_explicit_signal() -> None:
    detection = detect_engine(
        {"package.json": b'{"dependencies":{"mysql2":"3"}}'}
    )

    assert detection.proposed_engine == "mysql"
    assert {(signal.engine, signal.source) for signal in detection.signals} == {
        ("mysql", "package_dependency"),
    }


def test_conflicting_dependencies_do_not_propose_an_engine() -> None:
    detection = detect_engine(
        {"package.json": b'{"dependencies":{"mysql2":"3","pg":"8"}}'}
    )

    assert detection.proposed_engine is None
    assert detection.ambiguous is True
    assert {(signal.engine, signal.source) for signal in detection.signals} == {
        ("mysql", "package_dependency"),
        ("postgres", "package_dependency"),
    }


def test_hostile_agent_preview_configuration_is_not_an_engine_signal() -> None:
    detection = detect_engine(
        {
            ".agent/preview.yaml": b"services:\n  database:\n    type: mysql\n",
            ".env": b"DATABASE_URL=file:./local.sqlite\n",
        }
    )

    assert detection.proposed_engine == "sqlite"
    assert [signal.path for signal in detection.signals] == [".env"]


def test_tracked_database_file_is_reported_without_reading_project_code() -> None:
    detection = detect_engine(
        {".env": b"DATABASE_URL=file:./prisma/dev.db\n"},
        tracked_paths=("prisma/dev.db",),
    )

    assert detection.proposed_engine == "sqlite"
    assert detection.tracked_database_paths == ("prisma/dev.db",)


def test_untracked_database_file_is_not_reported() -> None:
    detection = detect_engine(
        {".env": b"DATABASE_URL=file:./prisma/dev.db\n"},
        tracked_paths=(),
    )

    assert detection.tracked_database_paths == ()


def test_discovery_container_is_read_only_networkless_and_reads_no_project_code(
    fake_docker_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    create = fake_docker_client.containers.create

    def capture(**kwargs: object):
        calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", capture)
    discover_engine(
        fake_docker_client,
        image="alpine:3.21",
        volume_name="workspace",
    )

    call = calls[0]
    assert call["read_only"] is True
    assert call["network_mode"] == "none"
    assert call["volumes"] == {"workspace": {"bind": "/workspace", "mode": "ro"}}
    assert ".agent/preview.yaml" not in str(call["command"])
    assert "source " not in str(call["command"])
    assert "git ls-files" in str(call["command"])
    assert fake_docker_client.created[0].removed is True
