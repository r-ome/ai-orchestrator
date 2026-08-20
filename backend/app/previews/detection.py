import difflib
import hashlib
import json
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.previews.models import (
    PreviewConfiguration,
    PreviewDependencyService,
    PreviewEnvironmentSource,
    PreviewInitialization,
    PreviewMode,
    PreviewNetworkAccess,
    PreviewPersistence,
    PreviewRuntime,
    PreviewServiceType,
    ProtectedFileChange,
)
from app.sandboxes.engine_detection import prisma_schema_providers

COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
PACKAGE_LOCKS = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)
PYTHON_DEPENDENCY_NAMES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
)
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".next",
        ".nuxt",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "coverage",
        "__pycache__",
    }
)
ENVIRONMENT_FILE_NAMES = (
    ".env",
    ".env.local",
    ".env.example",
    ".env.local.example",
    ".env.sample",
)
_ENVIRONMENT_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PRISMA_ENV_PATTERN = re.compile(r"env\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\)")
_MAX_ENVIRONMENT_LINE_BYTES = 4096
_MAX_ENVIRONMENT_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class DetectionResult:
    mode: PreviewMode
    runtime: PreviewRuntime
    confidence: str
    evidence: list[str]
    available_services: list[str]
    config: PreviewConfiguration
    required_environment: list[str] = field(default_factory=list)


def is_protected_runtime_file(path: str) -> bool:
    relative = PurePosixPath(path)
    name = relative.name
    if path == ".agent/preview.yaml":
        return True
    if name in COMPOSE_NAMES or name.startswith("Dockerfile"):
        return True
    if name == ".dockerignore" or name == "package.json":
        return True
    if name == "schema.prisma":
        return True
    if name in PACKAGE_LOCKS or name in PYTHON_DEPENDENCY_NAMES:
        return True
    return bool(
        re.fullmatch(
            r"(?:astro|vite|next)\.config\.(?:js|mjs|cjs|ts|mts|cts)",
            name,
        )
    )


def is_detection_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return is_protected_runtime_file(path) or name == "index.html"


def capture_source_runtime_files(
    root: Path,
    *,
    maximum_file_bytes: int,
    maximum_snapshot_bytes: int,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        if depth > 5:
            continue
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if entry.name not in IGNORED_DIRECTORIES:
                    pending.append((entry, depth + 1))
                continue
            if not stat.S_ISREG(metadata.st_mode) or not is_detection_file(relative):
                continue
            if metadata.st_size > maximum_file_bytes:
                continue
            content = entry.read_bytes()
            total += len(content)
            if total > maximum_snapshot_bytes:
                raise ValueError("Protected runtime files exceed the snapshot limit")
            files[relative] = content
    return files


def hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {
        path: hashlib.sha256(content).hexdigest()
        for path, content in sorted(files.items())
        if is_protected_runtime_file(path)
    }


def compare_files(
    current: dict[str, bytes],
    baseline: dict[str, tuple[bytes, str]],
) -> list[ProtectedFileChange]:
    changes: list[ProtectedFileChange] = []
    current_hashes = hashes(current)
    for path in sorted(set(current_hashes) | set(baseline)):
        current_hash = current_hashes.get(path, "")
        baseline_content, baseline_hash = baseline.get(path, (b"", ""))
        if current_hash == baseline_hash:
            continue
        if not baseline_hash:
            change = "added"
        elif not current_hash:
            change = "removed"
        else:
            change = "modified"
        changes.append(
            ProtectedFileChange(
                path=path,
                change=change,
                current_hash=current_hash,
                baseline_hash=baseline_hash,
                diff=_text_diff(path, baseline_content, current.get(path, b"")),
            )
        )
    return changes


def detect_preview(
    files: dict[str, bytes],
    *,
    default_expiry_minutes: int,
    environment_names: list[str] | None = None,
) -> DetectionResult:
    result = _detect_preview(files, default_expiry_minutes)
    required = sorted(
        set(environment_names or []) | set(schema_environment_names(files))
    )
    return replace(result, required_environment=required)


def _detect_preview(
    files: dict[str, bytes], default_expiry_minutes: int
) -> DetectionResult:
    manual = _manual_configuration(files, default_expiry_minutes)
    if manual is not None:
        return manual

    compose_file = next((name for name in COMPOSE_NAMES if name in files), "")
    if compose_file:
        return _detect_compose(files, compose_file, default_expiry_minutes)

    dockerfiles = sorted(
        path for path in files if PurePosixPath(path).name.startswith("Dockerfile")
    )
    if dockerfiles:
        dockerfile = "Dockerfile" if "Dockerfile" in dockerfiles else dockerfiles[0]
        port = _dockerfile_port(files[dockerfile]) or 8000
        return DetectionResult(
            mode=PreviewMode.DOCKERFILE,
            runtime=PreviewRuntime.UNKNOWN,
            confidence="medium",
            evidence=[dockerfile],
            available_services=[],
            config=PreviewConfiguration(
                mode=PreviewMode.DOCKERFILE,
                dockerfile=dockerfile,
                container_port=port,
                network_access=PreviewNetworkAccess.ISOLATED,
                expiry_minutes=default_expiry_minutes,
            ),
        )

    paths = set(files)
    astro_files = _framework_config_files(paths, "astro")
    vite_files = _framework_config_files(paths, "vite")
    next_files = _framework_config_files(paths, "next")
    if astro_files or _has_package_dependency(files, "astro"):
        evidence = [*astro_files[:1]]
        if "package.json" in paths:
            evidence.append("package.json")
        return _native(
            PreviewRuntime.ASTRO,
            "high",
            evidence,
            "node:22-alpine",
            _node_install_command(paths),
            "npm run dev -- --host 0.0.0.0",
            4321,
            default_expiry_minutes,
            files,
        )
    if vite_files:
        return _native(
            PreviewRuntime.VITE,
            "high",
            [vite_files[0], *(["package.json"] if "package.json" in paths else [])],
            "node:22-alpine",
            _node_install_command(paths),
            "npm run dev -- --host 0.0.0.0",
            5173,
            default_expiry_minutes,
            files,
        )
    if next_files:
        return _native(
            PreviewRuntime.NEXTJS,
            "high",
            [next_files[0], *(["package.json"] if "package.json" in paths else [])],
            "node:22-alpine",
            _node_install_command(paths),
            "npm run dev -- --hostname 0.0.0.0",
            3000,
            default_expiry_minutes,
            files,
        )
    if _has_fastapi(files):
        evidence = [
            path
            for path in ("pyproject.toml", "requirements.txt", "uv.lock")
            if path in files
        ]
        return _native(
            PreviewRuntime.FASTAPI,
            "medium",
            evidence,
            "python:3.12-slim",
            _python_install_command(paths),
            "uvicorn main:app --host 0.0.0.0 --port 8000",
            8000,
            default_expiry_minutes,
            files,
        )
    if "index.html" in files:
        return _native(
            PreviewRuntime.STATIC,
            "high",
            ["index.html"],
            "python:3.12-alpine",
            "",
            "python -m http.server 8000 --bind 0.0.0.0",
            8000,
            default_expiry_minutes,
            files,
        )

    database_schema, services, initialize, environment = _native_dependencies(files)
    return DetectionResult(
        mode=PreviewMode.UNKNOWN,
        runtime=PreviewRuntime.UNKNOWN,
        confidence="low",
        evidence=[database_schema] if database_schema else [],
        available_services=[],
        config=PreviewConfiguration(
            mode=PreviewMode.UNKNOWN,
            runtime=PreviewRuntime.UNKNOWN,
            image="",
            install_command="",
            start_command="",
            container_port=8000,
            host_port=None,
            selected_service="",
            compose_file="",
            dockerfile="",
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=default_expiry_minutes,
            services=services,
            initialize=initialize,
            environment=environment,
        ),
    )


def proposal_digest(
    config: PreviewConfiguration, protected_hashes: dict[str, str]
) -> str:
    payload = {
        "config": config.model_dump(mode="json"),
        "protected_files": protected_hashes,
        "environment": sorted(config.environment),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_environment_names(contents: dict[str, bytes]) -> list[str]:
    names: set[str] = set()
    for path in ENVIRONMENT_FILE_NAMES:
        content = contents.get(path)
        if content is None:
            continue
        names.update(_parse_dotenv_pairs(content))
    return sorted(names)


def parse_environment_pairs(contents: dict[str, bytes]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for path in ENVIRONMENT_FILE_NAMES:
        content = contents.get(path)
        if content is None:
            continue
        pairs.update(_parse_dotenv_pairs(content))
    return pairs


def schema_environment_names(files: dict[str, bytes]) -> list[str]:
    names: set[str] = set()
    for path, content in files.items():
        if PurePosixPath(path).name != "schema.prisma":
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        names.update(match.group(1) for match in _PRISMA_ENV_PATTERN.finditer(text))
    return sorted(names)


def _parse_dotenv_pairs(content: bytes) -> dict[str, str]:
    if len(content) > _MAX_ENVIRONMENT_FILE_BYTES:
        return {}
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    pairs: dict[str, str] = {}
    for raw_line in text.splitlines():
        if len(raw_line.encode("utf-8")) > _MAX_ENVIRONMENT_LINE_BYTES:
            continue
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not _ENVIRONMENT_KEY_PATTERN.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs[key] = value
    return pairs


def _manual_configuration(
    files: dict[str, bytes],
    default_expiry_minutes: int,
) -> DetectionResult | None:
    path = ".agent/preview.yaml"
    if path not in files:
        return None
    try:
        document = yaml.safe_load(files[path].decode("utf-8")) or {}
        if not isinstance(document, dict):
            return None
        data = dict(document)
        data.setdefault("expiry_minutes", default_expiry_minutes)
        data.setdefault("network_access", PreviewNetworkAccess.ISOLATED)
        config = PreviewConfiguration.model_validate(data)
    except (UnicodeDecodeError, yaml.YAMLError, ValueError):
        return None
    return DetectionResult(
        mode=config.mode,
        runtime=config.runtime,
        confidence="high",
        evidence=[path],
        available_services=[],
        config=config,
    )


def _detect_compose(
    files: dict[str, bytes],
    compose_file: str,
    default_expiry_minutes: int,
) -> DetectionResult:
    try:
        document = yaml.safe_load(files[compose_file].decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        document = {}
    raw_services = document.get("services", {}) if isinstance(document, dict) else {}
    services = raw_services if isinstance(raw_services, dict) else {}
    available = [str(service) for service in services]
    selected = next(
        (
            str(name)
            for name, service in services.items()
            if isinstance(service, dict)
            and (service.get("ports") or service.get("expose"))
        ),
        available[0] if available else "app",
    )
    selected_config = services.get(selected, {}) if isinstance(services, dict) else {}
    port = _compose_service_port(selected_config) or 8000
    evidence = [compose_file]
    build = selected_config.get("build") if isinstance(selected_config, dict) else None
    if isinstance(build, dict) and isinstance(build.get("dockerfile"), str):
        evidence.append(
            str(PurePosixPath(str(build.get("context", "."))) / build["dockerfile"])
        )
    elif build:
        evidence.append("Dockerfile")
    return DetectionResult(
        mode=PreviewMode.COMPOSE,
        runtime=PreviewRuntime.UNKNOWN,
        confidence="high" if services else "low",
        evidence=evidence,
        available_services=available,
        config=PreviewConfiguration(
            mode=PreviewMode.COMPOSE,
            compose_file=compose_file,
            selected_service=selected,
            container_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=default_expiry_minutes,
        ),
    )


def _native(
    runtime: PreviewRuntime,
    confidence: str,
    evidence: list[str],
    image: str,
    install_command: str,
    start_command: str,
    port: int,
    expiry_minutes: int,
    files: dict[str, bytes],
) -> DetectionResult:
    database_schema, services, initialize, environment = _native_dependencies(files)
    if database_schema:
        evidence = [*evidence, database_schema]
    return DetectionResult(
        mode=PreviewMode.NATIVE,
        runtime=runtime,
        confidence=confidence,
        evidence=evidence,
        available_services=[],
        config=PreviewConfiguration(
            mode=PreviewMode.NATIVE,
            runtime=runtime,
            image=image,
            install_command=install_command,
            start_command=start_command,
            container_port=port,
            network_access=PreviewNetworkAccess.ISOLATED,
            expiry_minutes=expiry_minutes,
            services=services,
            initialize=initialize,
            environment=environment,
        ),
    )


def _native_dependencies(
    files: dict[str, bytes],
) -> tuple[
    str | None,
    dict[str, PreviewDependencyService],
    PreviewInitialization,
    dict[str, PreviewEnvironmentSource],
]:
    prisma = _prisma_schema_provider(files)
    if prisma is None:
        return None, {}, PreviewInitialization(), {}
    database_schema, provider = prisma
    # Preview databases remain MySQL-only until Phase 6c.  The generalized
    # detector is also used by sandbox lifecycle discovery, which can propose
    # the other providers without changing preview behaviour.
    if provider != "mysql":
        return None, {}, PreviewInitialization(), {}
    commands = ["npx prisma migrate deploy"]
    if _has_package_script(files, "db:seed:preview"):
        commands.append("npm run db:seed:preview")
    return (
        database_schema,
        {
            "database": PreviewDependencyService(
                type=PreviewServiceType.MYSQL,
                image="mysql:8.4",
                database="atc_preview",
                persistence=PreviewPersistence.EPHEMERAL,
            )
        },
        PreviewInitialization(commands=commands),
        {"DATABASE_URL": PreviewEnvironmentSource(from_service="database")},
    )


def _prisma_schema_provider(files: dict[str, bytes]) -> tuple[str, str] | None:
    """Return the first Prisma provider for preview compatibility callers."""
    providers = prisma_schema_providers(files)
    return providers[0] if providers else None


def _has_package_script(files: dict[str, bytes], name: str) -> bool:
    content = files.get("package.json")
    if content is None:
        return False
    try:
        package = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    scripts = package.get("scripts") if isinstance(package, dict) else None
    return isinstance(scripts, dict) and isinstance(scripts.get(name), str)


def _has_package_dependency(files: dict[str, bytes], name: str) -> bool:
    content = files.get("package.json")
    if content is None:
        return False
    try:
        package = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(package, dict):
        return False
    dependency_groups = (
        "dependencies",
        "devDependencies",
    )
    return any(
        isinstance(package.get(group), dict) and name in package[group]
        for group in dependency_groups
    )


def _framework_config_files(paths: set[str], framework: str) -> list[str]:
    pattern = re.compile(rf"{re.escape(framework)}\.config\.(?:js|mjs|cjs|ts|mts|cts)")
    return sorted(path for path in paths if pattern.fullmatch(PurePosixPath(path).name))


def _node_install_command(paths: set[str]) -> str:
    if "pnpm-lock.yaml" in paths:
        return "corepack enable && pnpm install --frozen-lockfile"
    if "yarn.lock" in paths:
        return "corepack enable && yarn install --immutable"
    if "package-lock.json" in paths or "npm-shrinkwrap.json" in paths:
        return "npm ci"
    return "npm install"


def _python_install_command(paths: set[str]) -> str:
    if "requirements.txt" in paths:
        return "python -m pip install --disable-pip-version-check -r requirements.txt"
    return "python -m pip install --disable-pip-version-check -e ."


def _has_fastapi(files: dict[str, bytes]) -> bool:
    for path, content in files.items():
        if PurePosixPath(path).name not in PYTHON_DEPENDENCY_NAMES:
            continue
        if re.search(rb"(?im)(?:^|[^a-z0-9_-])fastapi(?:[^a-z0-9_-]|$)", content):
            return True
    return False


def _dockerfile_port(content: bytes) -> int | None:
    match = re.search(rb"(?im)^\s*EXPOSE\s+(\d{1,5})(?:/tcp)?(?:\s|$)", content)
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65_535 else None


def _compose_service_port(service: Any) -> int | None:
    if not isinstance(service, dict):
        return None
    for value in list(service.get("ports") or []) + list(service.get("expose") or []):
        if isinstance(value, int):
            return value
        if isinstance(value, dict):
            target = value.get("target")
            if isinstance(target, int):
                return target
        text = str(value).split("/")[0]
        try:
            target = int(text.rsplit(":", maxsplit=1)[-1])
        except ValueError:
            continue
        if 1 <= target <= 65_535:
            return target
    return None


def _text_diff(path: str, before: bytes, after: bytes) -> str:
    try:
        before_lines = before.decode("utf-8").splitlines()
        after_lines = after.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return "Binary content changed."
    diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"baseline/{path}",
            tofile=f"sandbox/{path}",
            lineterm="",
        )
    )
    if len(diff) > 200:
        diff = [*diff[:200], "... diff truncated after 200 lines ..."]
    return "\n".join(diff)
