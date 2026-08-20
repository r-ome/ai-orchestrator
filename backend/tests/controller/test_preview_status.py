from app.controller.store.preview_status import PreviewStatus

EXPECTED_PREVIEW_STATUSES = {
    "PREPARING": "preparing",
    "RUNNING": "running",
    "RESTARTING": "restarting",
    "REBUILDING": "rebuilding",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "FAILED": "failed",
    "MISSING": "missing",
    "EXPIRED": "expired",
}


def test_preview_status_values() -> None:
    assert {
        status.name: status.value for status in PreviewStatus
    } == EXPECTED_PREVIEW_STATUSES


def test_preview_status_names_are_closed() -> None:
    assert set(PreviewStatus.__members__) == set(EXPECTED_PREVIEW_STATUSES)
