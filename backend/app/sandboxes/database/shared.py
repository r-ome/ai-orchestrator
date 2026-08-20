import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from docker.models.containers import Container

from .constants import SHARED_DATABASE_PREFIX
from .contracts import ErrorFactory

_shared_database_locks_guard = Lock()
_shared_database_locks: dict[str, Lock] = {}


@contextmanager
def shared_database_server_lock(server_name: str) -> Iterator[None]:
    """Serialize one shared server without blocking unrelated projects."""
    with _shared_database_locks_guard:
        lock = _shared_database_locks.get(server_name)
        if lock is None:
            lock = Lock()
            _shared_database_locks[server_name] = lock
    with lock:
        yield


def mysql_shared_database_names(project_key: str) -> dict[str, str]:
    """Return stable shared-server resources for one project and engine.

    The name carries the engine, so a future engine change cannot reuse MySQL
    data or a server.
    """
    key = project_key[:12]
    prefix = f"{SHARED_DATABASE_PREFIX}{key}-mysql"
    return {
        "container": prefix,
        "data": f"{prefix}-data",
        "credentials": f"{prefix}-credentials",
        "network": f"{prefix}-net",
    }


def postgres_shared_database_names(project_key: str) -> dict[str, str]:
    """Return stable PostgreSQL shared-server resource names.

    The name carries the engine, matching `mysql_shared_database_names`.
    """
    key = project_key[:12]
    prefix = f"{SHARED_DATABASE_PREFIX}{key}-postgres"
    return {
        "container": prefix,
        "data": f"{prefix}-data",
        "credentials": f"{prefix}-credentials",
        "network": f"{prefix}-net",
    }


def shared_database_names(project_key: str, engine: str) -> dict[str, str]:
    """Resolve server resource names without assigning any SQLite resources."""
    if engine == "mysql":
        return mysql_shared_database_names(project_key)
    if engine == "postgres":
        return postgres_shared_database_names(project_key)
    raise ValueError(f"Database engine {engine!r} has no shared server")


def mysql_shared_schema_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{mysql_identifier(sandbox_id, error)}"


def mysql_shared_user_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{mysql_identifier(sandbox_id, error)}"


def mysql_identifier(sandbox_id: str, error: ErrorFactory) -> str:
    """Reduce a sandbox id to characters MySQL accepts unquoted."""
    cleaned = re.sub(r"[^a-z0-9]+", "", sandbox_id.casefold())[:16]
    if not cleaned:
        raise error(422, "Sandbox id cannot name a database schema")
    return cleaned


def postgres_identifier(sandbox_id: str, error: ErrorFactory) -> str:
    """Return the conservative unquoted identifier shared by role and database."""
    cleaned = re.sub(r"[^a-z0-9]+", "", sandbox_id.casefold())[:16]
    if not cleaned:
        raise error(422, "Sandbox id cannot name a PostgreSQL database")
    return cleaned


def postgres_shared_database_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{postgres_identifier(sandbox_id, error)}"


def postgres_shared_role_name(sandbox_id: str, error: ErrorFactory) -> str:
    return f"sbx_{postgres_identifier(sandbox_id, error)}"


def postgres_provision_statements(
    sandbox_id: str,
    password: str,
    error: ErrorFactory,
) -> list[str]:
    """Build root-only SQL for one sandbox database and login role."""
    name = postgres_shared_database_name(sandbox_id, error)
    escaped_password = password.replace("'", "''")
    return [
        f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{escaped_password}'",
        f'CREATE DATABASE "{name}" OWNER "{name}"',
    ]


def postgres_drop_statements(sandbox_id: str, error: ErrorFactory) -> list[str]:
    """Terminate clients before dropping the sandbox database and role."""
    name = postgres_shared_database_name(sandbox_id, error)
    escaped_name = name.replace("'", "''")
    return [
        (
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{escaped_name}' AND pid <> pg_backend_pid()"
        ),
        f'DROP DATABASE IF EXISTS "{name}"',
        f'DROP ROLE IF EXISTS "{name}"',
    ]


def wait_for_mysql_health(
    container: Container,
    *,
    timeout_seconds: int,
    error: ErrorFactory,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State") or {}
        status = str(state.get("Status") or container.status)
        health = str((state.get("Health") or {}).get("Status") or "")
        if status == "running" and health == "healthy":
            return
        if status in {"dead", "exited"} or health == "unhealthy":
            logs = container.logs(stdout=True, stderr=True, tail=100)
            detail = (
                logs.decode("utf-8", errors="replace")
                if isinstance(logs, bytes)
                else str(logs)
            )[-8_192:]
            raise error(422, f"MySQL failed its health check: {detail}")
        time.sleep(0.5)
    raise error(408, f"MySQL health check exceeded {timeout_seconds} seconds")


def schema_baseline_hash(files: dict[str, bytes]) -> str:
    """Hash sorted path-and-byte pairs without parsing project content."""
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        encoded_path = path.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
