import hashlib
import sqlite3
from pathlib import Path

import pytest
from conftest import register_ready_v1_sandbox

from app.controller.store import ActiveAgentRunExists, ControllerStore
from app.previews.detection import (
    compare_files,
    detect_preview,
    hashes,
    is_protected_runtime_file,
    parse_environment_names,
    parse_environment_pairs,
    schema_environment_names,
)
from app.previews.errors import PreviewOperationError
from app.previews.models import (
    PreviewConfiguration,
    PreviewMode,
    PreviewPersistence,
    PreviewRuntime,
)
from app.previews.progress import _record_preview_progress
from app.previews.runtimes.compose import (
    _compose_environment,
    _service_order,
    _validate_compose_service,
)
from app.previews.runtimes.native import _native_runtime_environment


def test_compose_wins_when_project_also_has_a_dockerfile() -> None:
    files = {
        "compose.yaml": b"""
services:
  database:
    image: postgres:17
  web:
    build: .
    ports:
      - "3000:8080"
""",
        "Dockerfile": b"FROM node:22\nEXPOSE 8080\n",
        "package.json": b'{"scripts":{"dev":"vite"}}',
        "vite.config.ts": b"export default {}",
    }

    result = detect_preview(files, default_expiry_minutes=30)

    assert result.mode is PreviewMode.COMPOSE
    assert result.config.selected_service == "web"
    assert result.config.container_port == 8080
    assert result.available_services == ["database", "web"]


@pytest.mark.parametrize(
    ("files", "runtime", "port"),
    [
        ({"index.html": b"hello"}, PreviewRuntime.STATIC, 8000),
        (
            {
                "package.json": b"{}",
                "package-lock.json": b"{}",
                "vite.config.ts": b"export default {}",
            },
            PreviewRuntime.VITE,
            5173,
        ),
        (
            {
                "package.json": b'{"dependencies":{"astro":"^5.11.0"}}',
                "package-lock.json": b"{}",
                "astro.config.mjs": b"export default {}",
            },
            PreviewRuntime.ASTRO,
            4321,
        ),
        (
            {"requirements.txt": b"fastapi==1.0\nuvicorn==1.0\n"},
            PreviewRuntime.FASTAPI,
            8000,
        ),
    ],
)
def test_detects_supported_native_runtimes(
    files: dict[str, bytes],
    runtime: PreviewRuntime,
    port: int,
) -> None:
    result = detect_preview(files, default_expiry_minutes=30)

    assert result.mode is PreviewMode.NATIVE
    assert result.runtime is runtime
    assert result.config.container_port == port


def test_astro_native_defaults_and_package_detection() -> None:
    files = {
        "package.json": b'{"devDependencies":{"astro":"^5.11.0"}}',
        "package-lock.json": b"{}",
    }

    result = detect_preview(files, default_expiry_minutes=30)

    assert result.mode is PreviewMode.NATIVE
    assert result.runtime is PreviewRuntime.ASTRO
    assert result.evidence == ["package.json"]
    assert result.config.image == "node:22-alpine"
    assert result.config.install_command == "npm ci"
    assert result.config.start_command == "npm run dev -- --host 0.0.0.0"
    assert result.config.container_port == 4321
    assert is_protected_runtime_file("astro.config.mjs")
    assert _native_runtime_environment(result.config) == {
        "ASTRO_TELEMETRY_DISABLED": "1"
    }


def test_prisma_mysql_is_an_approved_native_database_suggestion() -> None:
    files = {
        "package.json": b'{"scripts":{"db:seed:preview":"node seed.js"}}',
        "package-lock.json": b"{}",
        "vite.config.ts": b"export default {}",
        "prisma/schema.prisma": b"""
datasource db {
  provider = "mysql"
  url      = env("DATABASE_URL")
}
""",
    }

    result = detect_preview(files, default_expiry_minutes=30)

    database = result.config.services["database"]
    assert result.mode is PreviewMode.NATIVE
    assert result.evidence[-1] == "prisma/schema.prisma"
    assert database.type.value == "mysql"
    assert database.image == "mysql:8.4"
    assert database.persistence is PreviewPersistence.EPHEMERAL
    assert result.config.initialize.commands == [
        "npx prisma migrate deploy",
        "npm run db:seed:preview",
    ]
    assert result.config.environment["DATABASE_URL"].from_service == "database"


def test_prisma_postgresql_does_not_suggest_mysql() -> None:
    files = {
        "package.json": b"{}",
        "vite.config.ts": b"export default {}",
        "prisma/schema.prisma": b"""
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}
""",
    }

    result = detect_preview(files, default_expiry_minutes=30)

    assert result.config.services == {}
    assert result.config.initialize.commands == []
    assert result.config.environment == {}


def test_prisma_mysql_suggestion_does_not_require_a_known_app_runtime() -> None:
    result = detect_preview(
        {
            "prisma/schema.prisma": b"""
datasource db {
  provider = "mysql"
  url      = env("DATABASE_URL")
}
"""
        },
        default_expiry_minutes=30,
    )

    assert result.mode is PreviewMode.UNKNOWN
    assert result.evidence == ["prisma/schema.prisma"]
    assert result.config.services["database"].type.value == "mysql"


def test_database_configuration_requires_controller_database_url() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        PreviewConfiguration.model_validate(
            {
                "mode": "native",
                "runtime": "vite",
                "image": "node:22-alpine",
                "start_command": "npm run dev",
                "container_port": 5173,
                "services": {
                    "database": {
                        "type": "mysql",
                        "image": "mysql:8.4",
                        "database": "atc_preview",
                        "persistence": "ephemeral",
                    }
                },
            }
        )


def test_manual_manifest_is_a_suggestion() -> None:
    files = {
        ".agent/preview.yaml": b"""
mode: native
runtime: unknown
image: node:22-alpine
start_command: npm start -- --host 0.0.0.0
container_port: 4000
network_access: isolated
"""
    }

    result = detect_preview(files, default_expiry_minutes=30)

    assert result.mode is PreviewMode.NATIVE
    assert result.evidence == [".agent/preview.yaml"]
    assert result.config.container_port == 4000


def test_protected_file_comparison_reports_a_reviewable_diff() -> None:
    before = b'{"dependencies": {}}\n'
    after = b'{"dependencies": {"unexpected": "1.0.0"}}\n'
    baseline = {
        "package.json": (before, hashlib.sha256(before).hexdigest()),
    }

    changes = compare_files({"package.json": after}, baseline)

    assert changes[0].change == "modified"
    assert "unexpected" in changes[0].diff
    assert hashes({"package.json": after})["package.json"] == changes[0].current_hash


@pytest.mark.parametrize(
    "service",
    [
        {"image": "app", "privileged": True},
        {"image": "app", "network_mode": "host"},
        {"image": "app", "devices": ["/dev/kvm"]},
        {"image": "app", "env_file": ".env"},
    ],
)
def test_rejects_dangerous_compose_capabilities(service: dict[str, object]) -> None:
    with pytest.raises(PreviewOperationError, match="blocked fields"):
        _validate_compose_service("app", service)


def test_compose_environment_never_inherits_controller_values() -> None:
    with pytest.raises(PreviewOperationError, match="explicit values"):
        _compose_environment(["SECRET_FROM_CONTROLLER"])
    with pytest.raises(PreviewOperationError, match="interpolation"):
        _compose_environment({"TOKEN": "${HOST_TOKEN}"})


def test_orders_compose_dependencies_before_the_preview_service() -> None:
    order = _service_order(
        {
            "web": {"depends_on": ["api"]},
            "api": {"depends_on": {"database": {"condition": "service_started"}}},
            "database": {},
        }
    )

    assert order == ["database", "api", "web"]


def test_sqlite_enforces_one_active_agent_per_sandbox(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        volume_name="sample-volume",
        created_at="2026-08-04T00:00:00Z",
    )
    store.start_agent_run(
        run_id="agent-1",
        sandbox_id="sandbox-1",
        provider="codex",
    )

    with pytest.raises(ActiveAgentRunExists):
        store.start_agent_run(
            run_id="agent-2",
            sandbox_id="sandbox-1",
            provider="claude",
        )


def test_approved_removal_becomes_the_new_protected_baseline(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        volume_name="sample-volume",
        created_at="2026-08-04T00:00:00Z",
    )
    original = b'{"scripts":{"dev":"vite"}}'
    store.record_initial_baseline(
        "sandbox-1",
        {"package.json": original},
        {"package.json": hashlib.sha256(original).hexdigest()},
    )
    store.create_review(
        review_id="review-1",
        sandbox_id="sandbox-1",
        proposal_digest="a" * 64,
        detected_mode="unknown",
        config={},
        protected_files={},
        changes=[],
        created_at="2026-08-04T00:01:00Z",
        expires_at="2026-08-04T01:01:00Z",
    )

    store.approve_review(
        review_id="review-1",
        sandbox_id="sandbox-1",
        proposal_digest="b" * 64,
        config={},
        actor="human",
        files={"index.html": b"safe static page"},
        hashes={},
    )

    assert store.latest_baseline("sandbox-1")["package.json"] == (b"", "")


def test_sqlite_enforces_one_active_preview_per_sandbox(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        volume_name="sample-volume",
        created_at="2026-08-04T00:00:00Z",
    )

    def values(run_id: str, port: int) -> dict[str, object]:
        return {
            "id": run_id,
            "sandbox_id": "sandbox-1",
            "proposal_id": f"proposal-{run_id}",
            "mode": "native",
            "status": "running",
            "selected_service": "app",
            "container_port": 8000,
            "host_port": port,
            "config_json": "{}",
            "config_digest": "a" * 64,
            "network_name": f"network-{run_id}",
            "created_at": "2026-08-04T00:00:00Z",
            "started_at": "2026-08-04T00:00:01Z",
            "expires_at": "2026-08-04T00:30:00Z",
            "last_activity_at": "2026-08-04T00:00:01Z",
        }

    store.create_preview_run(values("preview-1", 41001))

    with pytest.raises(sqlite3.IntegrityError):
        store.create_preview_run(values("preview-2", 41002))


def test_preview_progress_events_are_persistent_and_ordered(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    register_ready_v1_sandbox(
        store,
        sandbox_id="sandbox-1",
        project_id="project-1",
        project_name="sample-sandbox-1",
        volume_name="sample-volume",
        created_at="2026-08-04T00:00:00Z",
    )

    _record_preview_progress(
        store,
        sandbox_id="sandbox-1",
        proposal_id="proposal-1",
        preview_id="preview-1",
        status="preparing",
        step="dependencies",
        message="Installing dependencies",
    )
    _record_preview_progress(
        store,
        sandbox_id="sandbox-1",
        proposal_id="proposal-1",
        preview_id="preview-1",
        status="failed",
        step="failed",
        message="npm returned code 1",
        level="error",
    )

    events = store.events_for_run("proposal-1", kind="preview.progress")

    assert [event["payload"]["step"] for event in events] == [
        "dependencies",
        "failed",
    ]
    assert events[-1]["payload"]["status"] == "failed"
    assert events[-1]["payload"]["message"] == "npm returned code 1"


ATC_DOTENV = b"""
# DATABASE_URL="mysql://old-user:old-pass@localhost:3306/atc"
DATABASE_URL="mysql://real-user:real-pass@localhost:3306/atc"
# DATABASE_URL="mysql://another-user:another-pass@localhost:3306/atc"
export NEXTAUTH_SECRET=supersecret
NEXTAUTH_URL='http://localhost:3000'
AWS_REGION=us-east-1
AWS_S3_CONTAINER_REPORTS_BUCKET=atc-reports
"""


def test_parse_environment_pairs_handles_atc_shaped_dotenv() -> None:
    pairs = parse_environment_pairs({".env": ATC_DOTENV})

    assert pairs == {
        "DATABASE_URL": "mysql://real-user:real-pass@localhost:3306/atc",
        "NEXTAUTH_SECRET": "supersecret",
        "NEXTAUTH_URL": "http://localhost:3000",
        "AWS_REGION": "us-east-1",
        "AWS_S3_CONTAINER_REPORTS_BUCKET": "atc-reports",
    }


def test_parse_environment_names_matches_pairs_keys() -> None:
    names = parse_environment_names({".env": ATC_DOTENV})

    assert names == sorted(
        [
            "DATABASE_URL",
            "NEXTAUTH_SECRET",
            "NEXTAUTH_URL",
            "AWS_REGION",
            "AWS_S3_CONTAINER_REPORTS_BUCKET",
        ]
    )


def test_parse_environment_pairs_skips_malformed_key() -> None:
    content = b"1INVALID=nope\nVALID_NAME=ok\n"

    pairs = parse_environment_pairs({".env": content})

    assert pairs == {"VALID_NAME": "ok"}


def test_parse_environment_pairs_later_file_overrides_earlier() -> None:
    contents = {
        ".env": b"SHARED=from-env\nONLY_IN_ENV=env-value\n",
        ".env.sample": b"SHARED=from-sample\n",
    }

    pairs = parse_environment_pairs(contents)

    assert pairs["SHARED"] == "from-sample"
    assert pairs["ONLY_IN_ENV"] == "env-value"


def test_parse_environment_pairs_ignores_oversized_file() -> None:
    content = b"A=1\n" + b"#" * (256 * 1024 + 1)

    pairs = parse_environment_pairs({".env": content})

    assert pairs == {}


def test_schema_environment_names_extracts_prisma_env_calls() -> None:
    schema = b"""
datasource db {
  provider = "mysql"
  url      = env("DATABASE_URL")
}

model User {
  id   Int    @id
  name String
}
"""
    names = schema_environment_names({"prisma/schema.prisma": schema})

    assert names == ["DATABASE_URL"]


def test_detect_preview_required_environment_merges_dotenv_and_prisma() -> None:
    files = {
        "prisma/schema.prisma": b"""
datasource db {
  provider = "mysql"
  url      = env("DATABASE_URL")
}
""",
    }

    result = detect_preview(
        files,
        default_expiry_minutes=30,
        environment_names=["NEXTAUTH_SECRET", "AWS_REGION"],
    )

    assert result.required_environment == [
        "AWS_REGION",
        "DATABASE_URL",
        "NEXTAUTH_SECRET",
    ]


def test_detect_preview_defaults_environment_names_to_empty() -> None:
    result = detect_preview({"index.html": b"hi"}, default_expiry_minutes=30)

    assert result.required_environment == []
