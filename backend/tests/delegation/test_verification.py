from types import SimpleNamespace

from app.delegation.packet import ResolvedVerification
from app.delegation.verification import VerificationSettings, run_verification


SETTINGS = VerificationSettings(
    image="verification-image",
    timeout_seconds=60,
    memory="2g",
    pids_limit=128,
    max_output_bytes=20,
)


class _Container:
    def __init__(self, exit_code: int, output: bytes = b"") -> None:
        self.exit_code = exit_code
        self.output = output
        self.started = False
        self.removed = False

    def start(self) -> None:
        self.started = True

    def wait(self, timeout: int):
        assert timeout == 60
        return {"StatusCode": self.exit_code}

    def logs(self, **_kwargs):
        return self.output

    def remove(self, force: bool = False) -> None:
        assert force
        self.removed = True


class _Containers:
    def __init__(self, containers: list[_Container]) -> None:
        self.pending = list(containers)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.pending.pop(0)


def _command(kind: str, command: str) -> ResolvedVerification:
    return ResolvedVerification(command_kind=kind, command=command)


def test_runs_confirmed_command_without_a_shell() -> None:
    containers = _Containers([_Container(0, b"clean")])
    client = SimpleNamespace(containers=containers)

    result = run_verification(
        client,
        SETTINGS,
        volume_name="project-volume",
        commands=[_command("test", "uv run pytest -q")],
    )

    assert result["passed"] is True
    assert containers.calls[0]["command"] == ["uv", "run", "pytest", "-q"]
    # `none` denies external connectivity while keeping /etc/hosts populated.
    # `network_disabled=True` empties it, and a build that resolves any name —
    # even `localhost` — then fails for a fault in the sandbox, not the code.
    assert containers.calls[0]["network_mode"] == "none"
    assert "network_disabled" not in containers.calls[0]
    assert containers.calls[0]["volumes"]["project-volume"]["mode"] == "rw"


def test_stops_after_first_failed_command_and_bounds_output() -> None:
    containers = _Containers([_Container(2, b"0123456789abcdefghijklmnopqrstuvwxyz")])
    client = SimpleNamespace(containers=containers)

    result = run_verification(
        client,
        SETTINGS,
        volume_name="project-volume",
        commands=[_command("build", "npm run build"), _command("test", "npm test")],
    )

    assert result["passed"] is False
    assert len(result["commands"]) == 1
    assert result["commands"][0]["exit_code"] == 2
    assert len(result["commands"][0]["output"]) == 20


def test_no_commands_does_not_pass() -> None:
    client = SimpleNamespace(containers=_Containers([]))

    assert run_verification(
        client,
        SETTINGS,
        volume_name="project-volume",
        commands=[],
    )["passed"] is False
