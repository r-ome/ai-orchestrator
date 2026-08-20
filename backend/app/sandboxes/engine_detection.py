"""Detect a project's database engine without running project code.

The reader mounts only the pinned sandbox workspace read-only and has no
network.  It intentionally does not inspect ``.agent/preview.yaml``: agents
can write that preview proposal, while this result later controls unattended
database recovery.
"""

import base64
import binascii
import io
import json
import re
import tarfile
from dataclasses import dataclass
from typing import Any

import yaml
from docker.client import DockerClient
from docker.errors import DockerException

from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened
from app.platform.labels import LABEL_CONTROLLER_MANAGED, LABEL_KIND
from app.previews.detection import prisma_schema_providers

MAX_FILE_BYTES = 200_000
MAX_LOG_BYTES = 16 * 1_048_576
_SECTION = "@@@ENGINE_FILE:"
_TRACKED_DATABASE_SECTION = "@@@ENGINE_TRACKED_DATABASE:"
_COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
_KNOWN_ENGINES = frozenset({"mysql", "postgres", "sqlite"})
NO_DATABASE = "none"
_EXPLICIT_PRECEDENCE_MAX = 3
_ENGINE_ALIASES = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mysql2": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
}


@dataclass(frozen=True)
class EngineSignal:
    engine: str
    source: str
    path: str
    detail: str
    precedence: int

    def as_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "source": self.source,
            "path": self.path,
            "detail": self.detail,
            "precedence": self.precedence,
        }


@dataclass(frozen=True)
class EngineDetection:
    signals: tuple[EngineSignal, ...]
    proposed_engine: str | None
    migrate_commands: tuple[str, ...]
    seed_commands: tuple[str, ...]
    commands_source: dict[str, str]
    tracked_database_paths: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return len({signal.engine for signal in self.signals}) > 1


def normalize_engine(value: str) -> str | None:
    """Map framework spellings to the controller's engine names."""
    return _ENGINE_ALIASES.get(value.strip().lower())


def normalize_confirmable_engine(value: str) -> str | None:
    """Map a human-confirmed engine spelling to its stored name."""
    if value.strip().lower() == NO_DATABASE:
        return NO_DATABASE
    return normalize_engine(value)


def detect_engine(
    files: dict[str, bytes],
    *,
    tracked_paths: tuple[str, ...] = (),
) -> EngineDetection:
    """Apply the database signal ladder to controller-read project files."""
    signals: list[EngineSignal] = []
    for path, provider in prisma_schema_providers(files):
        _append_signal(signals, provider, "prisma", path, f"provider = {provider}", 1)

    for path in (".env", ".env.example"):
        content = files.get(path)
        if content is None:
            continue
        for value in _dotenv_database_urls(content):
            engine = _url_engine(value)
            if engine is not None:
                _append_signal(signals, engine, "dotenv", path, "DATABASE_URL", 2)

    for path, content in sorted(files.items()):
        if path.endswith(".py"):
            for engine in _django_engines(content):
                _append_signal(signals, engine, "django", path, "DATABASES.ENGINE", 3)
        elif path.endswith("config/database.yml"):
            for engine in _rails_engines(content):
                _append_signal(signals, engine, "rails", path, "adapter", 3)
        elif path.endswith("alembic.ini"):
            for engine in _alembic_engines(content):
                _append_signal(signals, engine, "alembic", path, "sqlalchemy.url", 3)

    for path, content in sorted(files.items()):
        if path.rsplit("/", 1)[-1] not in _COMPOSE_NAMES:
            continue
        for engine, image in _compose_engines(content):
            _append_signal(signals, engine, "compose", path, f"image = {image}", 4)

    for path, content in sorted(files.items()):
        for engine, dependency in _dependency_engines(path, content):
            _append_signal(signals, engine, "package_dependency", path, dependency, 5)

    ordered = tuple(
        sorted(
            signals,
            key=lambda item: (item.precedence, item.path, item.source, item.detail),
        )
    )
    explicit_signals = tuple(
        signal for signal in ordered if signal.precedence <= _EXPLICIT_PRECEDENCE_MAX
    )
    decision_signals = explicit_signals or ordered
    engines = {signal.engine for signal in decision_signals}
    if not signals and not tracked_paths:
        proposed_engine = NO_DATABASE
    else:
        proposed_engine = next(iter(engines)) if len(engines) == 1 else None
    migrate_commands, seed_commands, commands_source = _prisma_commands(files)
    return EngineDetection(
        signals=ordered,
        proposed_engine=(
            proposed_engine
            if proposed_engine in _KNOWN_ENGINES or proposed_engine == NO_DATABASE
            else None
        ),
        migrate_commands=migrate_commands,
        seed_commands=seed_commands,
        commands_source=commands_source,
        tracked_database_paths=tuple(sorted(set(tracked_paths))),
    )


def discover_engine(
    docker_client: DockerClient,
    *,
    image: str,
    volume_name: str,
    timeout_seconds: int = 60,
) -> EngineDetection:
    """Read selected project files in an isolated inspection container."""
    files = _read_project_files(
        docker_client,
        image=image,
        volume_name=volume_name,
        timeout_seconds=timeout_seconds,
    )
    tracked = _tracked_database_paths(files.pop(_TRACKED_DATABASE_SECTION, b""))
    return detect_engine(files, tracked_paths=tracked)


def discover_schema_baseline_files(
    docker_client: DockerClient,
    *,
    image: str,
    volume_name: str,
    timeout_seconds: int = 60,
) -> dict[str, bytes]:
    """Read schema, migration, and seed sources without parsing or executing them."""
    return _read_schema_files(
        docker_client,
        image=image,
        volume_name=volume_name,
        timeout_seconds=timeout_seconds,
    )


def _append_signal(
    signals: list[EngineSignal],
    raw_engine: str,
    source: str,
    path: str,
    detail: str,
    precedence: int,
) -> None:
    engine = normalize_engine(raw_engine)
    if engine is not None:
        signals.append(EngineSignal(engine, source, path, detail, precedence))


def _dotenv_database_urls(content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []
    values: list[str] = []
    for line in text.splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == "DATABASE_URL":
            values.append(value.strip().strip("'\""))
    return values


def _url_engine(value: str) -> str | None:
    lowered = value.lower()
    if lowered.startswith("mysql://"):
        return "mysql"
    if lowered.startswith(("postgres://", "postgresql://")):
        return "postgres"
    if lowered.startswith("file:"):
        return "sqlite"
    return None


def _django_engines(content: bytes) -> list[str]:
    return _backend_matches(content, r"django\.db\.backends\.([A-Za-z0-9_]+)")


def _alembic_engines(content: bytes) -> list[str]:
    return _backend_matches(content, r"(?:postgresql|postgres|mysql|sqlite)(?=[+:])")


def _backend_matches(content: bytes, pattern: str) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return [
        match.group(1) if match.lastindex else match.group(0)
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]


def _rails_engines(content: bytes) -> list[str]:
    try:
        document = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return []
    values: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            adapter = value.get("adapter")
            if isinstance(adapter, str):
                values.append(adapter)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    return values


def _compose_engines(content: bytes) -> list[tuple[str, str]]:
    try:
        document = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return []
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return []
    found: list[tuple[str, str]] = []
    for service in services.values():
        image = service.get("image") if isinstance(service, dict) else None
        if not isinstance(image, str):
            continue
        name = image.lower().split("@", 1)[0].split(":", 1)[0].rsplit("/", 1)[-1]
        engine = normalize_engine(name)
        if engine is not None:
            found.append((engine, image))
    return found


def _dependency_engines(path: str, content: bytes) -> list[tuple[str, str]]:
    names: set[str] = set()
    if path.rsplit("/", 1)[-1] == "package.json":
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = {}
        if isinstance(document, dict):
            for group in ("dependencies", "devDependencies", "optionalDependencies"):
                dependencies = document.get(group)
                if isinstance(dependencies, dict):
                    names.update(str(name).lower() for name in dependencies)
    elif path.rsplit("/", 1)[-1] in {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "Pipfile",
    }:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        for name in ("psycopg", "asyncpg", "pymysql"):
            if re.search(rf"(?im)(?<![a-z0-9_-]){name}(?![a-z0-9_-])", text):
                names.add(name)
    mapping = {
        "pg": "postgres",
        "mysql2": "mysql",
        "better-sqlite3": "sqlite",
        "psycopg": "postgres",
        "asyncpg": "postgres",
        "pymysql": "mysql",
    }
    return [(mapping[name], name) for name in sorted(names) if name in mapping]


def _prisma_commands(
    files: dict[str, bytes],
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    if not prisma_schema_providers(files):
        return (), (), {}
    migrate = ("npx prisma migrate deploy",)
    seed: tuple[str, ...] = ()
    source = {"migrate": "prisma"}
    package = files.get("package.json")
    if package is not None:
        try:
            scripts = json.loads(package).get("scripts", {})
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict) and isinstance(
            scripts.get("db:seed:preview"), str
        ):
            seed = ("npm run db:seed:preview",)
            source["seed"] = "package_json"
    return migrate, seed, source


def _read_schema_files(
    docker_client: DockerClient,
    *,
    image: str,
    volume_name: str,
    timeout_seconds: int,
) -> dict[str, bytes]:
    """Return complete schema inputs as path-and-byte pairs."""
    # The boundary returns text, so base64 preserves arbitrary source bytes.
    script = (
        "set -eu\n"
        "cd /workspace\n"
        "find . -type f \\( "
        "-path '*/migration/*' -o -path '*/migrations/*' "
        "-o -path '*/seed/*' -o -path '*/seeds/*' -o -path '*/fixtures/*' "
        "-o -iname '*schema*' -o -name 'alembic.ini' "
        "-o -name 'database.yml' -o -name 'structure.sql' \\) "
        "-not -path './.git/*' -not -path './node_modules/*' "
        "-not -path './.agent/*' -print > /tmp/schema-files\n"
        "if [ -s /tmp/schema-files ]; then "
        "tar -cf /tmp/schema-files.tar -T /tmp/schema-files; "
        "base64 /tmp/schema-files.tar; fi\n"
    )
    try:
        result = run_hardened(
            docker_client,
            HardenedRunSpec(
                image=image,
                command=["sh", "-c", script],
                entrypoint=[],
                egress=Egress.DENIED,
                working_dir="/workspace",
                environment={"HOME": "/tmp/home"},
                labels={
                    LABEL_CONTROLLER_MANAGED: "true",
                    LABEL_KIND: "schema-baseline",
                },
                volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
                tmpfs_size="16m",
                timeout_seconds=timeout_seconds,
                max_log_bytes=MAX_LOG_BYTES,
                capture=Capture.SEPARATE,
            ),
        )
        if result.timed_out:
            raise RuntimeError("Schema-baseline discovery timed out")
        if result.exit_code != 0:
            raise RuntimeError("Schema-baseline discovery failed")
        archive = base64.b64decode(result.stdout)
        if not archive:
            return {}
        files: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                path = member.name.removeprefix("./")
                source = tar.extractfile(member)
                if path and source is not None:
                    files[path] = source.read()
        return files
    except DockerException as error:
        raise RuntimeError(f"Schema-baseline discovery failed: {error}") from error
    except (binascii.Error, tarfile.TarError, ValueError) as error:
        raise RuntimeError("Schema-baseline archive is invalid") from error


def _read_project_files(
    docker_client: DockerClient,
    *,
    image: str,
    volume_name: str,
    timeout_seconds: int,
) -> dict[str, bytes]:
    # This command only lists and reads controller-selected files. It does not
    # source environment files, call package managers, or execute project code.
    predicates = " -o ".join(
        f"-name '{name}'"
        for name in (
            "schema.prisma",
            ".env",
            ".env.example",
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "Pipfile",
            "database.yml",
            "alembic.ini",
            "*.py",
            *_COMPOSE_NAMES,
        )
    )
    # The explicit predicate avoids glob expansion and omits agent-controlled preview metadata.
    script = (
        "cd /workspace\n"
        "if [ -d .git ]; then git ls-files -- '*.db' '*.sqlite' '*.sqlite3' '*.db3' "
        "| while IFS= read -r file; do printf '"
        + _TRACKED_DATABASE_SECTION
        + '%s\\n\' "$file"; done; fi\n'
        "find . -type f \\( " + predicates + " \\) "
        "-not -path './.git/*' -not -path './node_modules/*' -not -path './.agent/*' "
        "| while IFS= read -r file; do "
        'case "$file" in */config/database.yml|*.py|*/alembic.ini|*/schema.prisma|*/.env|*/.env.example|*/package.json|*/pyproject.toml|*/requirements.txt|*/requirements-dev.txt|*/Pipfile|*/compose.yaml|*/compose.yml|*/docker-compose.yaml|*/docker-compose.yml) ;; *) continue ;; esac; '
        "path=${file#./}; printf '" + _SECTION + '%s\\n\' "$path"; '
        f"head -c {MAX_FILE_BYTES} \"$file\"; printf '\\n'; "
        "done\n"
    )
    try:
        result = run_hardened(
            docker_client,
            HardenedRunSpec(
                image=image,
                command=["sh", "-c", script],
                entrypoint=[],
                egress=Egress.DENIED,
                working_dir="/workspace",
                environment={"HOME": "/tmp/home"},
                labels={
                    LABEL_CONTROLLER_MANAGED: "true",
                    LABEL_KIND: "engine-detection",
                },
                volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
                tmpfs_size="16m",
                timeout_seconds=timeout_seconds,
                max_log_bytes=MAX_LOG_BYTES,
                capture=Capture.SEPARATE,
            ),
        )
        if result.timed_out:
            return {}
        return _split(result.stdout.encode("utf-8"))
    except DockerException:
        return {}


def _split(stdout: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for block in stdout.split(_SECTION.encode()):
        if not block.strip():
            continue
        path, separator, body = block.partition(b"\n")
        if separator:
            files[path.decode("utf-8", errors="replace").strip()] = body
    tracked_paths = _tracked_database_paths(stdout)
    if tracked_paths:
        files[_TRACKED_DATABASE_SECTION] = stdout.partition(_SECTION.encode())[0]
    return files


def _tracked_database_paths(stdout: bytes) -> tuple[str, ...]:
    """Parse only ``git ls-files`` output, never project-provided configuration."""
    paths: list[str] = []
    marker = _TRACKED_DATABASE_SECTION.encode()
    # The shell prints tracked paths before the first selected project file.
    # Limiting parsing to that header prevents file contents that resemble the
    # marker from creating a false tracked-path report.
    header, _, _ = stdout.partition(_SECTION.encode())
    for line in header.splitlines():
        if line.startswith(marker):
            path = line[len(marker) :].decode("utf-8", errors="replace").strip()
            if path:
                paths.append(path)
    return tuple(paths)
