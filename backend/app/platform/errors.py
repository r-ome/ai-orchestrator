"""The one exception body every domain error shares.

Each domain keeps its own subclass so a handler can still catch just its
own failures.
"""


class OperationError(Exception):
    """An operation failed with an HTTP-shaped reason."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
