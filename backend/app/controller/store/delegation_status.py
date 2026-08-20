from enum import StrEnum


class DelegationStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    HALTED = "halted"
    ABANDONED = "abandoned"
