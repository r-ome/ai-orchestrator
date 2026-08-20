from dataclasses import replace

from docker.client import DockerClient

from app.controller.store import ControllerStore, SandboxAdmissionError
from app.controller.store.lifecycle_status import SandboxLifecycleStatus
from app.sandboxes.engine_detection import NO_DATABASE
from app.sandboxes.lifecycle import lifecycle_conflict_detail, lifecycle_lease
from app.sandboxes.manifest import read_manifest, transition_sandbox_lifecycle

from .coercion import _json_value, require_v1
from .errors import SandboxConflict, SandboxValidationError
from .outcomes import EngineConfirmation
from .provisioning import complete_database_provision


def confirm_engine(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    confirmation: EngineConfirmation,
) -> dict[str, object]:
    """Freeze a human-approved engine and resume the creation lifecycle."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(sandbox, sandbox_id, "does not support engine confirmation")
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if (
        manifest is None
        or manifest.lifecycle_status
        is not SandboxLifecycleStatus.AWAITING_ENGINE_CONFIRMATION
    ):
        raise SandboxConflict("Sandbox is not awaiting engine confirmation")
    detection = controller_store.sandbox_engine_detection(sandbox_id)
    if detection is None:
        raise SandboxConflict("Sandbox has no engine detection to confirm")
    try:
        # This is intentionally a fresh lifecycle lease. The create lease was
        # released before the human received the proposal.
        with lifecycle_lease(
            controller_store, sandbox_id, "confirm-engine", docker_client=docker_client
        ):
            _confirm_engine_snapshot(
                controller_store,
                sandbox_id=sandbox_id,
                confirmation=confirmation,
                detection=detection,
            )
            current = read_manifest(controller_store, sandbox_id)
            if current is None:
                raise RuntimeError(
                    "v1 sandbox manifest disappeared during engine confirmation"
                )
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    current,
                    db_engine=confirmation.engine,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.CREATING,
            )
            complete_database_provision(
                docker_client,
                controller_store,
                sandbox_id=sandbox_id,
                operation="confirm-engine",
                rebuild=False,
            )
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    return sandbox


def _confirm_engine_snapshot(
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    confirmation: EngineConfirmation,
    detection: dict[str, object],
) -> None:
    proposed_migrate = [
        str(value) for value in _json_value(detection["migrate_commands_json"], [])
    ]
    proposed_seed = [
        str(value) for value in _json_value(detection["seed_commands_json"], [])
    ]
    migrate = confirmation.migrate_commands or proposed_migrate
    seed = confirmation.seed_commands or proposed_seed
    if confirmation.engine != NO_DATABASE and not migrate and not seed:
        raise SandboxValidationError(
            "Engine confirmation requires project migration or seed commands when detection proposes none"
        )
    sources = confirmation.commands_source or {
        str(key): str(value)
        for key, value in _json_value(detection["commands_source"], {}).items()
    }
    required_sources = ({"migrate"} if migrate else set()) | (
        {"seed"} if seed else set()
    )
    if required_sources.difference(sources):
        raise SandboxValidationError(
            "commands_source must identify the source for every approved command set"
        )
    controller_store.confirm_sandbox_engine_detection(
        sandbox_id=sandbox_id,
        engine=confirmation.engine,
        migrate_commands=migrate,
        seed_commands=seed,
        commands_source=sources,
        actor=confirmation.actor,
    )
