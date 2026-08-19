from pathlib import Path
from threading import RLock

from app.controller.config import ControllerSettings, get_controller_settings

from .agents import AgentsMixin
from .connection import ConnectionMixin
from .errors import (
    ActiveAgentRunExists,
    AgentWriterSessionExists,
    ChangeRequestRunning,
    DelegationActive,
    OpenTaskExists,
    ReviewGenerating,
    RevisionTaken,
    RunActive,
    SandboxAdmissionError,
    SandboxLeaseBlockedByWriterError,
    SandboxLeaseHeldError,
    SandboxWriterAdmissionError,
    SlotTaken,
)
from .events import EventsMixin
from .implementation import ImplementationMixin
from .migrations import MIGRATIONS, _add_column  # noqa: F401
from .planning import PlanningMixin
from .previews import PreviewsMixin
from .projects import ProjectsMixin
from .reviews import ReviewsMixin
from .sandboxes import SandboxesMixin
from .schema import FIRST_V1_MIGRATION, INITIAL_MIGRATION
from .tasks import TasksMixin


class ControllerStore(
    ConnectionMixin,
    ProjectsMixin,
    SandboxesMixin,
    TasksMixin,
    ReviewsMixin,
    PlanningMixin,
    ImplementationMixin,
    AgentsMixin,
    PreviewsMixin,
    EventsMixin,
):
    """Serialized SQLite access for controller-owned intent and audit state."""


_stores: dict[Path, ControllerStore] = {}
_stores_lock = RLock()


def get_controller_store() -> ControllerStore:
    return controller_store_for_settings(get_controller_settings())


def controller_store_for_settings(settings: ControllerSettings) -> ControllerStore:
    path = settings.database_path
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = ControllerStore(path)
            store.initialize()
            _stores[path] = store
        return store


__all__ = [
    "ActiveAgentRunExists",
    "AgentWriterSessionExists",
    "ChangeRequestRunning",
    "ControllerStore",
    "DelegationActive",
    "FIRST_V1_MIGRATION",
    "INITIAL_MIGRATION",
    "MIGRATIONS",
    "OpenTaskExists",
    "ReviewGenerating",
    "RevisionTaken",
    "RunActive",
    "SandboxAdmissionError",
    "SandboxLeaseBlockedByWriterError",
    "SandboxLeaseHeldError",
    "SandboxWriterAdmissionError",
    "SlotTaken",
    "controller_store_for_settings",
    "get_controller_store",
]
