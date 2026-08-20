class SandboxConflict(Exception):
    """A sandbox state conflict that maps to HTTP 409."""

    status_code = 409

    def __init__(self, detail: object) -> None:
        self.detail = detail
        super().__init__(str(detail))


class SandboxDependencyFailure(Exception):
    """A dependency failure that maps to HTTP 424."""

    status_code = 424

    @property
    def detail(self) -> str:
        return str(self)


class SandboxNotFound(Exception):
    """A missing sandbox that maps to HTTP 404."""

    status_code = 404

    @property
    def detail(self) -> str:
        return str(self)


class SandboxUnavailable(Exception):
    """A sandbox dependency outage that maps to HTTP 503."""

    status_code = 503

    @property
    def detail(self) -> str:
        return str(self)


class SandboxInternalFailure(Exception):
    """An internal sandbox failure that maps to HTTP 500."""

    status_code = 500

    @property
    def detail(self) -> str:
        return str(self)


class SandboxValidationError(Exception):
    """A rejected sandbox request that maps to HTTP 422."""

    status_code = 422

    @property
    def detail(self) -> str:
        return str(self)
