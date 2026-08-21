import os
from dataclasses import dataclass, field
from functools import lru_cache

from app.containers.config import PreviewRuntimeLimits, get_preview_runtime_limits
from app.platform.env import integer_setting


@dataclass(frozen=True)
class PreviewSettings:
    inspection_image: str
    default_expiry_minutes: int
    maximum_file_bytes: int
    maximum_snapshot_bytes: int
    proposal_lifetime_seconds: int
    build_timeout_seconds: int
    maximum_dependency_bytes: int = 2_147_483_648
    maximum_built_image_bytes: int = 4_294_967_296
    limits: PreviewRuntimeLimits = field(default_factory=PreviewRuntimeLimits)


@lru_cache
def get_preview_settings() -> PreviewSettings:
    return PreviewSettings(
        inspection_image=os.getenv("PREVIEW_INSPECTION_IMAGE", "alpine:latest"),
        default_expiry_minutes=integer_setting("PREVIEW_DEFAULT_EXPIRY_MINUTES", 30),
        maximum_file_bytes=integer_setting("PREVIEW_MAXIMUM_FILE_BYTES", 1_048_576),
        maximum_snapshot_bytes=integer_setting(
            "PREVIEW_MAXIMUM_SNAPSHOT_BYTES", 16_777_216
        ),
        proposal_lifetime_seconds=integer_setting(
            "PREVIEW_PROPOSAL_LIFETIME_SECONDS", 900
        ),
        build_timeout_seconds=integer_setting("PREVIEW_BUILD_TIMEOUT_SECONDS", 900),
        maximum_dependency_bytes=integer_setting(
            "PREVIEW_MAXIMUM_DEPENDENCY_BYTES",
            2_147_483_648,
        ),
        maximum_built_image_bytes=integer_setting(
            "PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES",
            4_294_967_296,
        ),
        limits=get_preview_runtime_limits(),
    )
