from enum import StrEnum


class PreviewStatus(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    RESTARTING = "restarting"
    REBUILDING = "rebuilding"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    MISSING = "missing"
    EXPIRED = "expired"


#: Ordered to match the one_active_preview_per_sandbox index in existing
#: databases. REBUILDING stays so legacy database rows still parse.
ACTIVE_PREVIEW_STATUSES: tuple[PreviewStatus, ...] = (
    PreviewStatus.PREPARING,
    PreviewStatus.RUNNING,
    PreviewStatus.RESTARTING,
    PreviewStatus.STOPPING,
)

# Render the tuple as a SQLite IN-list. Its exact text must match the index in existing databases.
ACTIVE_PREVIEW_STATUS_SQL = ", ".join(
    f"'{status}'" for status in ACTIVE_PREVIEW_STATUSES
)

TERMINAL_PREVIEW_STATUSES: frozenset[PreviewStatus] = frozenset(
    {
        PreviewStatus.STOPPED,
        PreviewStatus.FAILED,
        PreviewStatus.MISSING,
        PreviewStatus.EXPIRED,
    }
)
