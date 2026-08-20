class SandboxDatabaseError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class SandboxMigrationError(SandboxDatabaseError):
    """The approved snapshot failed inside the restricted runtime."""
