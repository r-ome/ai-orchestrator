import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class PreviewSettings:
    inspection_image: str
    default_expiry_minutes: int
    maximum_file_bytes: int
    maximum_snapshot_bytes: int
    proposal_lifetime_seconds: int
    prepare_timeout_seconds: int
    build_timeout_seconds: int
    maximum_dependency_bytes: int = 2_147_483_648
    maximum_built_image_bytes: int = 4_294_967_296
    # A dev-mode bundler compiling a large page is the peak here, not the
    # served application. One gibibyte kills Next.js mid-compile.
    preview_memory: str = "4g"
    # One shared server carries every sandbox of a project, so it gets more
    # headroom than the per-sandbox server it replaces.
    shared_database_memory: str = "2g"
    shared_database_max_connections: int = 200


@lru_cache
def get_preview_settings() -> PreviewSettings:
    return PreviewSettings(
        inspection_image=os.getenv("PREVIEW_INSPECTION_IMAGE", "alpine:latest"),
        default_expiry_minutes=_integer("PREVIEW_DEFAULT_EXPIRY_MINUTES", 30),
        maximum_file_bytes=_integer("PREVIEW_MAXIMUM_FILE_BYTES", 1_048_576),
        maximum_snapshot_bytes=_integer("PREVIEW_MAXIMUM_SNAPSHOT_BYTES", 16_777_216),
        proposal_lifetime_seconds=_integer("PREVIEW_PROPOSAL_LIFETIME_SECONDS", 900),
        prepare_timeout_seconds=_integer("PREVIEW_PREPARE_TIMEOUT_SECONDS", 600),
        build_timeout_seconds=_integer("PREVIEW_BUILD_TIMEOUT_SECONDS", 900),
        maximum_dependency_bytes=_integer(
            "PREVIEW_MAXIMUM_DEPENDENCY_BYTES",
            2_147_483_648,
        ),
        maximum_built_image_bytes=_integer(
            "PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES",
            4_294_967_296,
        ),
        preview_memory=os.getenv("PREVIEW_MEMORY", "4g"),
        shared_database_memory=os.getenv("PREVIEW_SHARED_DATABASE_MEMORY", "2g"),
        shared_database_max_connections=_integer(
            "PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS",
            200,
        ),
    )


def _integer(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default
