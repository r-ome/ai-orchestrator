import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
# Models only. `delegation.service` imports this module, so reaching for
# anything further into that package would close a ring.
from app.delegation.models import DelegationStatus
from app.implementation_context import prompts
from app.implementation_context.config import ContextSettings
from app.implementation_context.inventory import (
    CommandInventory,
    confirm_command,
    discover_inventory,
    parse_inventory,
)
from app.implementation_context.models import (
    COMMAND_KINDS,
    ContextManifest,
    ContextStatus,
    GenerateContextRequest,
    GenerateContextOutcome,
    ImplementationContext,
    ResolvedCommand,
)
from app.implementation_context.validators import validate_context_payload
from app.planning.config import PlanningSettings
from app.planning.models import PlanningRole, PlanningStatus
from app.planning.runner import (
    PlanningTurnError,
    TurnRequest,
    TurnResult,
    run_planning_turn,
)


_CONTEXT_SESSION_STATUSES = {
    PlanningStatus.PLAN_READY.value,
    PlanningStatus.REVIEW_LIMIT_REACHED.value,
}


class ContextOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class _ContextTurn:
    result: TurnResult | None
    attempts: int
    validation_errors: list[str]
    error: str | None = None


@dataclass(frozen=True)
class ContextClaim:
    """A generating context row plus everything its turn still needs.

    Claiming is separated from running so the HTTP caller gets its 404, 409, or
    the new row's id straight away, while the turn — minutes of container time —
    goes to `app.jobs`. The claimed row is the job record: progress arrives as
    `context.progress` events on it.
    """

    context_id: str
    session_id: str
    sandbox_id: str
    title: str
    plan: dict[str, Any]
    volume_name: str
    turn_settings: PlanningSettings
    provider: AgentProvider


def claim_context(
    store: ControllerStore,
    planning_settings: PlanningSettings,
    settings: ContextSettings,
    session_id: str,
    request: GenerateContextRequest,
    *,
    project_name: str | None = None,
) -> ContextClaim:
    """Open the session's context for generation, or raise the reason it cannot."""
    session = _planning_session(store, session_id, project_name=project_name)
    if str(session["status"]) not in _CONTEXT_SESSION_STATUSES:
        raise ContextOperationError(
            409,
            "Implementation context needs a completed plan, "
            f"but this session is '{session['status']}'",
        )
    plan = _json(session.get("plan_spec_json"))
    if plan is None:
        raise ContextOperationError(409, "Planning session has no plan specification")

    sandbox_id = str(session["sandbox_id"])
    sandbox = store.sandbox(sandbox_id)
    if sandbox is None:
        raise ContextOperationError(404, f"Sandbox '{sandbox_id}' was not found")

    turn_settings, model = _effective_turn_settings(
        planning_settings,
        settings,
        session,
        request,
    )

    _refuse_if_delegated(store, session_id)

    context_id = store.start_implementation_context(
        {
            "id": uuid4().hex,
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "status": ContextStatus.GENERATING.value,
            "provider": request.provider.value,
            "model": model,
        }
    )
    if context_id is None:
        raise ContextOperationError(
            409,
            "A context is already being generated for this session",
        )

    _progress(
        store,
        context_id,
        sandbox_id,
        step="claimed",
        message="Implementation context reserved",
    )
    return ContextClaim(
        context_id=context_id,
        session_id=session_id,
        sandbox_id=sandbox_id,
        title=str(session["title"]),
        plan=plan,
        volume_name=str(sandbox["volume_name"]),
        turn_settings=turn_settings,
        provider=request.provider,
    )


def _refuse_if_delegated(store: ControllerStore, session_id: str) -> None:
    """Freeze the context once work items exist.

    There are no context revisions, so regenerating overwrites the row a
    delegation's packets were built from. Rather than keep revisions to make
    that safe, the context stops being writable at the point anything depends
    on it. An abandoned delegation does not count: nothing is running.
    """
    live = [
        row
        for row in store.delegations_for_session(session_id)
        if str(row["status"]) != DelegationStatus.ABANDONED.value
    ]
    if not live:
        return
    raise ContextOperationError(
        409,
        "This session already has a delegation built from its implementation "
        "context. Abandon the delegation before regenerating the context.",
    )


def execute_context(
    docker_client: DockerClient,
    settings: ContextSettings,
    store: ControllerStore,
    claim: ContextClaim,
) -> GenerateContextOutcome:
    """Run the turn behind a claim and settle its row. Safe to run off-request."""
    try:
        _progress(
            store,
            claim.context_id,
            claim.sandbox_id,
            step="inventory",
            message="Discovering runnable commands in the sandbox",
        )
        inventory = discover_inventory(
            docker_client,
            image=settings.git_image,
            volume_name=claim.volume_name,
            timeout_seconds=settings.inventory_timeout_seconds,
        )
        prompt = prompts.context_prompt(
            claim.title,
            claim.plan,
            _available(inventory),
            inventory,
        )
        _progress(
            store,
            claim.context_id,
            claim.sandbox_id,
            step="turn",
            message=f"Running the {claim.provider.value} context turn",
        )
        turn = _run_context_turn(
            docker_client,
            claim.turn_settings,
            claim.provider,
            claim.session_id,
            claim.volume_name,
            prompt,
            inventory,
        )
    except Exception as error:
        store.settle_implementation_context(
            claim.context_id,
            to_status=ContextStatus.FAILED.value,
            changes={"error": (str(error) or "Context turn did not complete")[:900]},
        )
        _progress(
            store,
            claim.context_id,
            claim.sandbox_id,
            step="failed",
            message=str(error) or "Context turn did not complete",
            level="error",
        )
        raise

    outcome = _settle(store, claim.context_id, turn, inventory)
    _progress(
        store,
        claim.context_id,
        claim.sandbox_id,
        step="settled",
        message=(
            "Implementation context is ready"
            if outcome.accepted
            else "Context turn produced no usable output"
        ),
        level="info" if outcome.accepted else "error",
    )
    return outcome


def fail_claim(store: ControllerStore, claim: ContextClaim, detail: str) -> None:
    """Settle a claim whose turn never started. See jobs.submit_docker_job."""
    store.settle_implementation_context(
        claim.context_id,
        to_status=ContextStatus.FAILED.value,
        changes={"error": detail[:900]},
    )
    _progress(
        store,
        claim.context_id,
        claim.sandbox_id,
        step="failed",
        message=detail,
        level="error",
    )


def _progress(
    store: ControllerStore,
    context_id: str,
    sandbox_id: str,
    *,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    store.event(
        sandbox_id=sandbox_id,
        run_id=context_id,
        kind="context.progress",
        payload={"step": step, "message": message[:900], "level": level},
    )


def generate_context(
    docker_client: DockerClient,
    planning_settings: PlanningSettings,
    settings: ContextSettings,
    store: ControllerStore,
    session_id: str,
    request: GenerateContextRequest,
    *,
    project_name: str | None = None,
) -> GenerateContextOutcome:
    """Generate the session's implementation context, start to finish."""
    claim = claim_context(
        store,
        planning_settings,
        settings,
        session_id,
        request,
        project_name=project_name,
    )
    return execute_context(docker_client, settings, store, claim)


def _run_context_turn(
    docker_client: DockerClient,
    settings: PlanningSettings,
    provider: AgentProvider,
    session_id: str,
    volume_name: str,
    prompt: str,
    inventory: CommandInventory,
) -> _ContextTurn:
    current_prompt = prompt
    validation_errors: list[str] = []

    for attempt in range(1, 3):
        try:
            result = run_planning_turn(
                docker_client,
                settings,
                TurnRequest(
                    role=PlanningRole.IMPLEMENTATION_CONTEXT,
                    provider=provider,
                    prompt=current_prompt,
                    project_volume=volume_name,
                    session_id=session_id,
                ),
            )
            errors = validate_context_payload(result.payload)
            errors += _command_errors(result.payload, inventory)
            raw_output = result.raw_output
        except PlanningTurnError as error:
            if error.status_code != 422:
                raise
            result = None
            errors = [error.detail]
            raw_output = error.raw_output

        validation_errors.extend(
            error for error in errors if error not in validation_errors
        )
        if not errors or attempt == 2:
            return _ContextTurn(
                result=result,
                attempts=attempt,
                validation_errors=validation_errors,
                error=errors[0] if result is None and errors else None,
            )
        current_prompt = prompts.repair_prompt(prompt, errors, raw_output)

    raise AssertionError("context repair loop must return")


def _settle(
    store: ControllerStore,
    context_id: str,
    turn: _ContextTurn,
    inventory: CommandInventory,
) -> GenerateContextOutcome:
    result = turn.result
    if result is None:
        store.settle_implementation_context(
            context_id,
            to_status=ContextStatus.FAILED.value,
            changes={"error": (turn.error or "Turn produced no JSON object")[:900]},
        )
        return _outcome(store, context_id, turn, accepted=False)

    structural = validate_context_payload(result.payload)
    if structural:
        store.settle_implementation_context(
            context_id,
            to_status=ContextStatus.FAILED.value,
            changes={"error": "; ".join(structural)[:900], "model": result.model},
        )
        return _outcome(store, context_id, turn, accepted=False)

    commands = _resolve_commands(result.payload, inventory)
    store.settle_implementation_context(
        context_id,
        to_status=ContextStatus.READY.value,
        changes={
            "manifest_json": json.dumps(result.payload),
            "commands_json": json.dumps(
                [command.model_dump() for command in commands]
            ),
            "inventory_json": json.dumps(inventory.as_dict()),
            "model": result.model,
        },
    )
    return _outcome(store, context_id, turn, accepted=True)


def _resolve_commands(
    payload: Mapping[str, Any],
    inventory: CommandInventory,
) -> list[ResolvedCommand]:
    commands = payload.get("commands")
    if not isinstance(commands, Mapping):
        return []
    resolved: list[ResolvedCommand] = []
    for kind in COMMAND_KINDS:
        command = commands.get(kind)
        if not isinstance(command, str) or not command.strip():
            continue
        confirmed, reason = confirm_command(command, inventory)
        resolved.append(
            ResolvedCommand(
                kind=kind,
                command=command.strip(),
                confirmed=confirmed,
                reason=reason,
            )
        )
    return resolved


def _command_errors(
    payload: Mapping[str, Any],
    inventory: CommandInventory,
) -> list[str]:
    return [
        f"commands.{command.kind}: '{command.command}' — {command.reason}"
        for command in _resolve_commands(payload, inventory)
        if not command.confirmed
    ]


def _available(inventory: CommandInventory) -> list[str]:
    # `node_runner`, not a literal `npm`: proposing `npm run test` to a project
    # whose lockfile is pnpm's produces a command the controller then rejects,
    # and the turn spent its one repair fixing the controller's own suggestion.
    runner = inventory.node_runner
    available = [f"{runner} run {script}" for script in sorted(inventory.npm_scripts)]
    available += [f"make {target}" for target in sorted(inventory.make_targets)]
    if inventory.python_project:
        available.append("pytest")
    return available[:40]


def get_context(
    store: ControllerStore,
    context_id: str,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> ImplementationContext:
    row = store.implementation_context(context_id)
    if row is None or (session_id is not None and str(row["session_id"]) != session_id):
        raise ContextOperationError(404, "Implementation context was not found")
    if project_name is not None:
        _planning_session(store, str(row["session_id"]), project_name=project_name)
    return _context(row)


def session_context(
    store: ControllerStore,
    session_id: str,
    *,
    project_name: str | None = None,
) -> ImplementationContext | None:
    """The session's context in whatever state it is, or None before the first run."""
    _planning_session(store, session_id, project_name=project_name)
    row = store.implementation_context_for_session(session_id)
    return _context(row) if row is not None else None


def ready_context(
    store: ControllerStore,
    session_id: str,
) -> ImplementationContext | None:
    """The session's context, but only once its turn has landed a manifest."""
    row = store.implementation_context_for_session(session_id)
    if row is None or str(row["status"]) != ContextStatus.READY.value:
        return None
    return _context(row)


def _outcome(
    store: ControllerStore,
    context_id: str,
    turn: _ContextTurn,
    *,
    accepted: bool,
) -> GenerateContextOutcome:
    context = get_context(store, context_id)
    return GenerateContextOutcome(
        context=context,
        accepted=accepted,
        attempts=turn.attempts,
        validation_errors=turn.validation_errors,
        turn_status="succeeded" if turn.result is not None else "invalid_output",
        turn_error=turn.error,
        unconfirmed_commands=[
            command for command in context.commands if not command.confirmed
        ],
    )


def _context(row: Mapping[str, Any]) -> ImplementationContext:
    manifest = _json(row.get("manifest_json"))
    commands = _json_list(row.get("commands_json"))
    return ImplementationContext(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        sandbox_id=str(row["sandbox_id"]),
        status=str(row["status"]),
        manifest=ContextManifest.model_validate(manifest) if manifest else None,
        commands=[ResolvedCommand.model_validate(item) for item in commands],
        inventory=_json(row.get("inventory_json")),
        provider=row.get("provider"),
        model=row.get("model"),
        error=row.get("error"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=row.get("settled_at"),
    )


def _effective_turn_settings(
    planning_settings: PlanningSettings,
    context_settings: ContextSettings,
    session: Mapping[str, Any],
    request: GenerateContextRequest,
) -> tuple[PlanningSettings, str]:
    changes: dict[str, Any] = {
        "credential_profile": str(session["credential_profile"]),
    }
    if request.provider.value == "claude":
        model = request.model or context_settings.model
        changes["claude_model"] = model
    else:
        model = request.model or planning_settings.codex_model
        changes["codex_model"] = model
    return replace(planning_settings, **changes), model


def _planning_session(
    store: ControllerStore,
    session_id: str,
    *,
    project_name: str | None,
) -> dict[str, Any]:
    session = store.planning_session(session_id)
    if session is None:
        raise ContextOperationError(404, "Planning session was not found")
    if (
        project_name is not None
        and str(session["project_name"]).casefold() != project_name.casefold()
    ):
        raise ContextOperationError(404, "Planning session was not found")
    return session


def _json(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


__all__ = [
    "ContextClaim",
    "ContextOperationError",
    "claim_context",
    "execute_context",
    "fail_claim",
    "generate_context",
    "get_context",
    "ready_context",
    "session_context",
    "parse_inventory",
]
