from collections.abc import Iterable, Mapping
from typing import Any

from app.sandboxes.models import SandboxLifecycleStatus

class SandboxAdmissionError(RuntimeError):
    """Base error for persisted sandbox admission conflicts."""


class SandboxLeaseHeldError(SandboxAdmissionError):
    def __init__(self, sandbox_id: str, lease: Mapping[str, Any]) -> None:
        self.sandbox_id = sandbox_id
        self.lease = dict(lease)
        super().__init__(
            f"Sandbox '{sandbox_id}' is held by lifecycle operation "
            f"{lease['operation']} '{lease['operation_id']}'"
        )


class SandboxLeaseBlockedByWriterError(SandboxAdmissionError):
    def __init__(self, sandbox_id: str, writers: Iterable[Mapping[str, Any]]) -> None:
        self.sandbox_id = sandbox_id
        self.writers = [dict(writer) for writer in writers]
        writer = self.writers[0]
        self.writer_class = str(writer["writer_class"])
        self.writer_id = str(writer["writer_id"])
        super().__init__(
            f"Sandbox '{sandbox_id}' has active {self.writer_class} "
            f"writer '{self.writer_id}'"
        )


class SandboxWriterAdmissionError(SandboxAdmissionError):
    def __init__(
        self,
        sandbox_id: str,
        *,
        lease: Mapping[str, Any] | None = None,
        lifecycle_status: SandboxLifecycleStatus | None = None,
        desired_state: str | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.lease = dict(lease) if lease is not None else None
        self.lifecycle_status = lifecycle_status
        self.desired_state = desired_state
        if lease is not None:
            detail = (
                f"Sandbox '{sandbox_id}' is held by lifecycle operation "
                f"{lease['operation']} '{lease['operation_id']}'"
            )
        else:
            detail = (
                f"Sandbox '{sandbox_id}' does not admit writers while lifecycle status "
                f"is '{lifecycle_status}' and desired state is '{desired_state}'"
            )
            if lifecycle_status is SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION:
                detail += "; confirm the database engine to unblock it"
        super().__init__(detail)


class SlotTaken(RuntimeError):
    """A partial unique index refused a claim because the slot is occupied."""

    owner_label = "Sandbox"
    slot = "a taken slot"

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id
        # Keep the established attribute available to existing sandbox callers.
        self.sandbox_id = owner_id
        super().__init__(f"{self.owner_label} '{owner_id}' already has {self.slot}")


class OpenTaskExists(SlotTaken):
    slot = "an open task"


class ActiveAgentRunExists(SlotTaken):
    slot = "an active agent run"


class AgentWriterSessionExists(SlotTaken):
    slot = "an open agent writer session"


class RevisionTaken(SlotTaken):
    owner_label = "Owner"
    slot = "a concurrent revision claim"


class DelegationActive(SlotTaken):
    slot = "an active delegation"


class ReviewGenerating(SlotTaken):
    owner_label = "Delegation"
    slot = "a generating integration review"


class ChangeRequestRunning(SlotTaken):
    owner_label = "Delegation"
    slot = "a running change request"


class RunActive(SlotTaken):
    owner_label = "Delegation"
    slot = "a running work item run"
