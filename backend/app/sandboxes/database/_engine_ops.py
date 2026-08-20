"""Database engine operations."""

import base64
import json
import secrets
from typing import Any

from docker.client import DockerClient
from docker.errors import APIError, ContainerError, NotFound

from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened

from .constants import DATABASE_COMMAND_MAX_LOG_BYTES, DATABASE_COMMAND_TIMEOUT_SECONDS
from .contracts import DatabaseProvisionRequest, ErrorFactory
from .errors import SandboxDatabaseError


def _ensure_sqlite_volume(request: DatabaseProvisionRequest) -> Any:
    try:
        return request.docker_client.volumes.get(request.data_volume)
    except NotFound:
        pass
    try:
        return request.docker_client.volumes.create(
            name=request.data_volume,
            driver="local",
            labels=request.labels,
        )
    except APIError:
        return request.docker_client.volumes.get(request.data_volume)


def _read_or_create_server_credentials(
    docker_client: DockerClient,
    image: str,
    credentials_volume: Any,
    *,
    filename: str,
    create_missing: bool = True,
    error: ErrorFactory,
) -> dict[str, str]:
    """Read engine credentials without exposing a server root password to apps."""
    credential_path = f"/credentials/{filename}"

    def read_stored() -> dict[str, str] | None:
        output = _run_database_command(
            docker_client,
            image=image,
            command=[
                "sh",
                "-c",
                f"if [ -f {credential_path} ]; then cat {credential_path}; fi",
            ],
            volumes={credentials_volume.name: {"bind": "/credentials", "mode": "ro"}},
        )
        if not output:
            return None
        try:
            loaded = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error(
                409, "Stored persistent database credentials are invalid"
            ) from exc
        if not isinstance(loaded, dict) or any(
            not isinstance(loaded.get(key), str) or not loaded[key]
            for key in ("username", "password", "root_password")
        ):
            raise error(409, "Stored persistent database credentials are invalid")
        return loaded

    stored = read_stored()
    if stored is not None:
        return stored
    if not create_missing:
        raise error(409, "Stored database credentials are missing")
    credentials = {
        "username": f"preview_{secrets.token_hex(4)}",
        "password": secrets.token_urlsafe(24),
        "root_password": secrets.token_urlsafe(32),
    }
    encoded = base64.b64encode(
        json.dumps(credentials, separators=(",", ":")).encode()
    ).decode("ascii")
    _run_database_command(
        docker_client,
        image=image,
        command=[
            "sh",
            "-c",
            (
                "set -eu; umask 077; "
                "destination=/credentials/$DATABASE_CREDENTIAL_FILE; "
                "temporary=$(mktemp /credentials/.${DATABASE_CREDENTIAL_FILE}.XXXXXX); "
                "trap 'rm -f \"$temporary\"' EXIT; "
                'printf \'%s\' "$DATABASE_CREDENTIALS" | base64 -d > "$temporary"; '
                'if ! ln "$temporary" "$destination" 2>/dev/null; then '
                '[ -f "$destination" ] || exit 1; fi'
            ),
        ],
        environment={
            "DATABASE_CREDENTIALS": encoded,
            "DATABASE_CREDENTIAL_FILE": filename,
        },
        volumes={credentials_volume.name: {"bind": "/credentials", "mode": "rw"}},
    )
    stored = read_stored()
    if stored is None:
        raise error(409, "Stored database credentials are missing")
    return stored


def _run_database_command(
    docker_client: DockerClient,
    *,
    image: str,
    command: list[str],
    environment: dict[str, str] | None = None,
    volumes: dict[str, Any] | None = None,
    network: str | None = None,
    tmpfs_size: str = "256m",
) -> str:
    """Run one bounded administrative command and keep `ContainerError` semantics."""
    result = run_hardened(
        docker_client,
        HardenedRunSpec(
            image=image,
            command=command,
            environment=environment or {},
            volumes=volumes or {},
            network=network,
            egress=Egress.DENIED,
            tmpfs_size=tmpfs_size,
            capture=Capture.SEPARATE,
            timeout_seconds=DATABASE_COMMAND_TIMEOUT_SECONDS,
            max_log_bytes=DATABASE_COMMAND_MAX_LOG_BYTES,
        ),
    )
    if result.timed_out:
        # A killed container leaves no exit code, so ContainerError would
        # report the timeout as "non-zero exit status None".
        raise SandboxDatabaseError(
            504,
            f"Database command exceeded {DATABASE_COMMAND_TIMEOUT_SECONDS} seconds",
        )
    if result.exit_code != 0:
        raise ContainerError(None, result.exit_code, command, image, result.stderr)
    return result.stdout
