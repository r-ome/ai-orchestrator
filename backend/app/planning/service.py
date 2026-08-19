import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

import docker
from docker.client import DockerClient

from app.agents.models import AgentProvider
from app.controller.store import ControllerStore
from app.delegation.config import get_routing_settings
from app.planning.config import PlanningSettings, reasoning_effort_choices
from app.planning.feature_status import derive_feature_status
from app.planning.models import (
    TERMINAL_PLANNING_STATUSES,
    CreatePlanningSessionRequest,
    FindingStatus,
    PlanningFinding,
    PlanningMessage,
    PlanningMessageRaw,
    PlanningMessageRequest,
    PlanningRole,
    PlanningSession,
    PlanningSessionDetail,
    PlanningSessionsResponse,
    PlanningStatus,
    PlanSpec,
    source_statuses,
)
from app.planning.prompts import (
    clarifier_prompt,
    feature_brief,
    planner_prompt,
    reviewer_prompt,
)
from app.planning.runner import (
    PlanningTurnError,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    run_planning_turn,
    run_validated_turn,
)
from app.platform.errors import OperationError
from app.projects.service import (
    ProjectOperationError,
    ensure_sandbox_registered,
    inspect_registered_project,
    project_id,
)


class PlanningOperationError(OperationError):
    """A planning operation failed."""


class TurnKind(StrEnum):
    CLARIFIER = "clarifier"
    PLANNER = "planner"
    REVIEWER = "reviewer"


_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
_ACTIVE_FINDING_STATUSES = {
    FindingStatus.OPEN.value,
    FindingStatus.ANSWERED.value,
    FindingStatus.REJECTED.value,
}
_TRANSIENT_SCAN_LINES = 5
_TRANSIENT_TURN_FAILURE_PHRASES = (
    "at capacity",
    "try a different model",
    "rate limit",
    "rate_limit",
    "overloaded",
    "temporarily unavailable",
    "service unavailable",
)


def _accepted(outcome: TurnOutcome) -> TurnResult:
    if outcome.accepted and outcome.result is not None:
        return outcome.result
    raise PlanningTurnError(
        422,
        "; ".join(outcome.errors) or "the turn produced no usable output",
        outcome.result.raw_output if outcome.result else "",
    )


def create_session(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    settings: PlanningSettings,
    project_name: str,
    request: CreatePlanningSessionRequest,
) -> PlanningSession:
    try:
        sandbox_id, project_key, project = ensure_sandbox_registered(
            docker_client,
            controller_store,
            project_name,
        )
    except ProjectOperationError as error:
        raise PlanningOperationError(error.status_code, error.detail) from error

    clarifier_provider = request.clarifier_provider or settings.clarifier_provider
    planner_provider = request.planner_provider or settings.planner_provider
    reviewer_provider = request.reviewer_provider or settings.reviewer_provider
    clarifier_model = _resolve_model(
        request.clarifier_model, clarifier_provider, settings
    )
    planner_model = _resolve_model(request.planner_model, planner_provider, settings)
    reviewer_model = _resolve_model(request.reviewer_model, reviewer_provider, settings)
    reviewer_reasoning_effort = _resolve_reviewer_reasoning_effort(request, settings)
    session_id = uuid4().hex
    controller_store.create_planning_session(
        session_id=session_id,
        project_id=project_key,
        sandbox_id=sandbox_id,
        project_name=project.name,
        title=request.title,
        status=PlanningStatus.CLARIFYING.value,
        clarifier_provider=clarifier_provider.value,
        planner_provider=planner_provider.value,
        reviewer_provider=reviewer_provider.value,
        clarifier_model=clarifier_model,
        planner_model=planner_model,
        reviewer_model=reviewer_model,
        reviewer_reasoning_effort=reviewer_reasoning_effort,
        credential_profile=settings.credential_profile,
        max_review_turns=request.max_review_turns or settings.max_review_turns,
    )
    controller_store.append_planning_message(
        session_id=session_id,
        role=PlanningRole.USER.value,
        text=request.request,
    )
    schedule_turn(controller_store, settings, session_id, TurnKind.CLARIFIER)
    return _session_model(_required_session(controller_store, session_id))


def list_sessions(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> PlanningSessionsResponse:
    try:
        project = inspect_registered_project(
            docker_client,
            project_name,
            controller_store,
        )
    except ProjectOperationError as error:
        raise PlanningOperationError(error.status_code, error.detail) from error
    sessions = [
        _session_model(session)
        for session in controller_store.planning_sessions_for_project(
            
                project.source_path.removeprefix("managed:")
                if project.source_path.startswith("managed:")
                else project_id(project.source_path)
            
        )
    ]
    return PlanningSessionsResponse(count=len(sessions), sessions=sessions)


def _resolve_model(
    requested: str | None,
    provider: AgentProvider,
    settings: PlanningSettings,
) -> str:
    if requested is not None:
        owner = get_routing_settings().provider_for_model(requested)
        if owner is not None and owner is not provider:
            raise PlanningOperationError(
                422,
                f"'{requested}' is a {owner.value} model and {provider.value} cannot run it.",
            )
        return requested
    return settings.claude_model if provider is AgentProvider.CLAUDE else settings.codex_model


def _resolve_reviewer_reasoning_effort(
    request: CreatePlanningSessionRequest,
    settings: PlanningSettings,
) -> str:
    """Check the operator's choice against exactly what the dialog offered.

    The dialog's list carries the deployment's configured effort when that sits
    outside the three standard ones. Validating against the same list keeps a
    round trip through the dialog from being refused.
    """
    if request.reviewer_reasoning_effort is None:
        return settings.codex_reasoning_effort
    choices = reasoning_effort_choices(settings)
    if request.reviewer_reasoning_effort not in choices:
        raise PlanningOperationError(
            422,
            f"Reviewer reasoning effort must be one of: {', '.join(choices)}",
        )
    return request.reviewer_reasoning_effort


def get_session(
    controller_store: ControllerStore,
    project_name: str,
    session_id: str,
) -> PlanningSessionDetail:
    session = _session_for_project(controller_store, project_name, session_id)
    session_with_feature_facts = controller_store.planning_session_with_feature_facts(session_id)
    if session_with_feature_facts is not None:
        session = session_with_feature_facts
    messages = [_message_model(row) for row in controller_store.planning_messages(session_id)]
    return PlanningSessionDetail(
        **_session_data(session),
        feature_brief=str(session["feature_brief"]),
        messages=messages,
        findings=[PlanningFinding(**_finding_data(row)) for row in controller_store.planning_findings(session_id)],
        plan_spec=_json_value(session.get("plan_spec_json")),
    )


def get_message_raw_output(
    controller_store: ControllerStore,
    project_name: str,
    session_id: str,
    sequence: int,
) -> PlanningMessageRaw:
    """Returns one message's raw agent output.

    Kept out of the session payload on purpose. The page polls that payload
    every two seconds while a session runs, and a turn's container log is far
    larger than the summary the page renders from.
    """
    _session_for_project(controller_store, project_name, session_id)
    row = controller_store.planning_message(session_id, sequence)
    if row is None:
        raise PlanningOperationError(404, "Planning message not found")
    return PlanningMessageRaw(
        sequence=int(row["sequence"]),
        role=PlanningRole(str(row["role"])),
        raw_output=str(row["raw_output"] or ""),
    )


def post_message(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    project_name: str,
    session_id: str,
    request: PlanningMessageRequest,
) -> PlanningSession:
    session = _session_for_project(controller_store, project_name, session_id)
    _reject_terminal(session)
    if session["turn_state"] == "running":
        raise PlanningOperationError(409, "A planning turn is already running for this session")
    if session["status"] == PlanningStatus.AWAITING_CONFIRMATION.value:
        raise PlanningOperationError(
            409,
            "This session awaits confirmation. Use confirm, correct, or proceed instead.",
        )
    if session["status"] != PlanningStatus.CLARIFYING.value:
        raise PlanningOperationError(409, "This session is no longer accepting clarifications")
    controller_store.append_planning_message(
        session_id=session_id,
        role=PlanningRole.USER.value,
        text=request.text,
    )
    schedule_turn(controller_store, settings, session_id, TurnKind.CLARIFIER)
    return _session_model(_required_session(controller_store, session_id))


def confirm_understanding(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    project_name: str,
    session_id: str,
) -> PlanningSession:
    session = _session_for_project(controller_store, project_name, session_id)
    _reject_terminal(session)
    if session["turn_state"] == "running":
        raise PlanningOperationError(409, "A planning turn is already running for this session")
    if session["status"] != PlanningStatus.AWAITING_CONFIRMATION.value:
        raise PlanningOperationError(409, "This session is not awaiting confirmation")
    _freeze_and_start_planning(controller_store, settings, session, confirmed=True)
    return _session_model(_required_session(controller_store, session_id))


def correct_understanding(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    project_name: str,
    session_id: str,
    request: PlanningMessageRequest,
) -> PlanningSession:
    session = _session_for_project(controller_store, project_name, session_id)
    _reject_terminal(session)
    if session["turn_state"] == "running":
        raise PlanningOperationError(409, "A planning turn is already running for this session")
    if session["status"] != PlanningStatus.AWAITING_CONFIRMATION.value:
        raise PlanningOperationError(409, "This session is not awaiting confirmation")
    controller_store.append_planning_message(
        session_id=session_id,
        role=PlanningRole.USER.value,
        text=request.text,
    )
    transition_planning_session(
        controller_store,
        session_id=session_id,
        to_status=PlanningStatus.CLARIFYING,
    )
    schedule_turn(controller_store, settings, session_id, TurnKind.CLARIFIER)
    return _session_model(_required_session(controller_store, session_id))


def proceed_without_confirmation(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    project_name: str,
    session_id: str,
) -> PlanningSession:
    session = _session_for_project(controller_store, project_name, session_id)
    _reject_terminal(session)
    if session["turn_state"] == "running":
        raise PlanningOperationError(409, "A planning turn is already running for this session")
    if session["status"] not in {
        PlanningStatus.CLARIFYING.value,
        PlanningStatus.AWAITING_CONFIRMATION.value,
    }:
        raise PlanningOperationError(409, "This session cannot proceed to planning")
    _freeze_and_start_planning(controller_store, settings, session, confirmed=False)
    return _session_model(_required_session(controller_store, session_id))


def cancel_session(
    controller_store: ControllerStore,
    project_name: str,
    session_id: str,
) -> PlanningSession:
    session = _session_for_project(controller_store, project_name, session_id)
    if not _is_terminal(session):
        transition_planning_session(
            controller_store,
            session_id=session_id,
            to_status=PlanningStatus.CANCELLED,
        )
    return _session_model(_required_session(controller_store, session_id))


def transition_planning_session(
    controller_store: ControllerStore,
    *,
    session_id: str,
    to_status: PlanningStatus,
    failure_reason: str | None = None,
) -> bool:
    """The only way a planning status changes. Sources come from PLANNING_TRANSITIONS.

    Callers name a destination, never a source, so a transition the table does
    not draw cannot be requested. The store turns the sources into the UPDATE's
    WHERE clause, which makes the check atomic rather than a read-then-write.
    """
    return controller_store.advance_planning_status(
        session_id=session_id,
        from_statuses=[status.value for status in source_statuses(to_status)],
        to_status=to_status.value,
        settled=to_status in TERMINAL_PLANNING_STATUSES,
        failure_reason=failure_reason,
    )


def schedule_turn(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
    kind: TurnKind,
) -> None:
    task = asyncio.get_running_loop().create_task(
        _run_turn(controller_store, settings, session_id, kind)
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _run_turn(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
    kind: TurnKind,
) -> None:
    if not controller_store.claim_planning_turn(session_id):
        return
    try:
        session = _required_session(controller_store, session_id)
        if _is_terminal(session):
            return
        if kind is TurnKind.CLARIFIER and session["status"] != PlanningStatus.CLARIFYING.value:
            return
        if kind is TurnKind.PLANNER and session["status"] != PlanningStatus.PLANNING.value:
            return
        if kind is TurnKind.REVIEWER and session["status"] != PlanningStatus.UNDER_REVIEW.value:
            return
        result = await _run_turn_with_retries(controller_store, settings, session_id, kind)
        if result is None:
            return

        session = _required_session(controller_store, session_id)
        if _is_terminal(session):
            _append_raw_system_message(controller_store, session_id, result.raw_output)
            return
        if kind is TurnKind.CLARIFIER:
            _apply_clarifier_result(controller_store, session, result)
        elif kind is TurnKind.PLANNER:
            _apply_planner_result(controller_store, settings, session, result)
        else:
            _apply_reviewer_result(controller_store, settings, session, result)
    finally:
        controller_store.release_planning_turn(session_id)


async def _run_turn_with_retries(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
    kind: TurnKind,
) -> TurnResult | None:
    max_attempts = settings.turn_retries + 1
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            session = _required_session(controller_store, session_id)
            if _is_terminal(session):
                return None
        try:
            return await asyncio.to_thread(
                _run_model_turn,
                controller_store,
                settings,
                session_id,
                kind,
            )
        except PlanningTurnError as error:
            if not _is_transient_turn_failure(error):
                _record_turn_error(controller_store, session_id, error)
                return None
            if attempt == max_attempts:
                _record_turn_error(
                    controller_store,
                    session_id,
                    PlanningTurnError(
                        error.status_code,
                        f"{error.detail} (after {attempt} attempts)",
                        error.raw_output,
                    ),
                )
                return None

            session = _required_session(controller_store, session_id)
            if _is_terminal(session):
                _append_raw_system_message(controller_store, session_id, error.raw_output)
                return None
            delay = settings.turn_retry_backoff_seconds * 2 ** (attempt - 1)
            _append_raw_system_message(
                controller_store,
                session_id,
                (
                    f"The {kind.value} turn hit a temporary upstream failure. "
                    f"Retrying attempt {attempt + 1} of {max_attempts} in {delay} seconds: "
                    f"{error.detail}"
                ),
            )
            await asyncio.sleep(delay)
        except Exception as error:
            _record_turn_error(
                controller_store,
                session_id,
                PlanningTurnError(502, f"{kind.value} turn failed: {error}"),
            )
            return None
    return None


def _is_transient_turn_failure(error: PlanningTurnError) -> bool:
    """Decide whether one failed turn is worth retrying.

    Only the tail is scanned. A provider prints its error last, immediately
    before exiting, while everything above is the echoed prompt and the model's
    own text. That prompt embeds the plan under review, and a plan about rate
    limiting legitimately contains these phrases without being one.

    The bias is deliberately toward retrying: a needless retry costs seconds,
    while a missed one throws away a session that has real work in it.
    """
    if error.status_code == 504:
        return False
    text = "\n".join(
        _tail_lines(source) for source in (error.detail, error.raw_output)
    ).lower()
    return any(phrase in text for phrase in _TRANSIENT_TURN_FAILURE_PHRASES)


def _tail_lines(source: str) -> str:
    lines = [line for line in source.splitlines() if line.strip()]
    return "\n".join(lines[-_TRANSIENT_SCAN_LINES:])


def _turn_client() -> DockerClient:
    return docker.from_env()


def _run_clarifier_turn(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
) -> TurnResult:
    session = _required_session(controller_store, session_id)
    sandbox = controller_store.sandbox(str(session["sandbox_id"]))
    if sandbox is None:
        raise PlanningTurnError(500, "clarifier turn has no registered sandbox")
    request = TurnRequest(
        role=PlanningRole.CLARIFIER,
        provider=AgentProvider(str(session["clarifier_provider"])),
        prompt=clarifier_prompt(
            title=str(session["title"]),
            messages=controller_store.planning_messages(session_id),
        ),
        project_volume=str(sandbox["volume_name"]),
        session_id=session_id,
    )
    client = _turn_client()
    try:
        return _accepted(run_validated_turn(
            lambda prompt: run_planning_turn(
                client,
                _turn_settings(settings, session, PlanningRole.CLARIFIER),
                replace(request, prompt=prompt),
            ),
            prompt=request.prompt,
            validate=_validate_clarifier_payload,
        ))
    finally:
        client.close()


def _run_model_turn(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
    kind: TurnKind,
) -> TurnResult:
    if kind is TurnKind.CLARIFIER:
        return _run_clarifier_turn(controller_store, settings, session_id)
    if kind is TurnKind.PLANNER:
        return _run_planner_turn(controller_store, settings, session_id)
    return _run_reviewer_turn(controller_store, settings, session_id)


def _turn_settings(
    settings: PlanningSettings,
    session: Mapping[str, Any],
    role: PlanningRole,
) -> PlanningSettings:
    provider = AgentProvider(str(session[f"{role.value}_provider"]))
    model = session.get(f"{role.value}_model")
    if provider is AgentProvider.CLAUDE:
        return replace(settings, claude_model=str(model or settings.claude_model))
    changes: dict[str, str] = {"codex_model": str(model or settings.codex_model)}
    if role is PlanningRole.REVIEWER:
        changes["codex_reasoning_effort"] = str(
            session.get("reviewer_reasoning_effort") or settings.codex_reasoning_effort
        )
    return replace(settings, **changes)


def _run_planner_turn(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
) -> TurnResult:
    session = _required_session(controller_store, session_id)
    sandbox = controller_store.sandbox(str(session["sandbox_id"]))
    if sandbox is None:
        raise PlanningTurnError(500, "planner turn has no registered sandbox")
    previous_turns = [
        message
        for message in controller_store.planning_messages(session_id)
        if message["role"] == PlanningRole.PLANNER.value
    ]
    ledger = _review_ledger(controller_store.planning_findings(session_id))
    request = TurnRequest(
        role=PlanningRole.PLANNER,
        provider=AgentProvider(str(session["planner_provider"])),
        prompt=planner_prompt(
            brief=str(session["feature_brief"]),
            round_number=int(session["plan_revision"]) + 1,
            previous_turns=previous_turns,
            ledger=ledger,
        ),
        project_volume=str(sandbox["volume_name"]),
        session_id=session_id,
    )
    client = _turn_client()
    try:
        return _accepted(run_validated_turn(
            lambda prompt: run_planning_turn(
                client,
                _turn_settings(settings, session, PlanningRole.PLANNER),
                replace(request, prompt=prompt),
            ),
            prompt=request.prompt,
            validate=_planner_validator({str(finding["id"]) for finding in ledger}),
        ))
    finally:
        client.close()


def _run_reviewer_turn(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session_id: str,
) -> TurnResult:
    session = _required_session(controller_store, session_id)
    sandbox = controller_store.sandbox(str(session["sandbox_id"]))
    if sandbox is None:
        raise PlanningTurnError(500, "reviewer turn has no registered sandbox")
    revision = _current_revision(controller_store, session_id, int(session["plan_revision"]))
    plan = _json_value(revision["plan_json"])
    if not isinstance(plan, dict):
        raise PlanningTurnError(500, "stored plan revision has invalid JSON")
    request = TurnRequest(
        role=PlanningRole.REVIEWER,
        provider=AgentProvider(str(session["reviewer_provider"])),
        prompt=reviewer_prompt(
            brief=str(session["feature_brief"]),
            plan=plan,
            ledger=_review_ledger(controller_store.planning_findings(session_id)),
        ),
        project_volume=str(sandbox["volume_name"]),
        session_id=session_id,
    )
    client = _turn_client()
    try:
        return _accepted(run_validated_turn(
            lambda prompt: run_planning_turn(
                client,
                _turn_settings(settings, session, PlanningRole.REVIEWER),
                replace(request, prompt=prompt),
            ),
            prompt=request.prompt,
            validate=_validate_reviewer_payload,
        ))
    finally:
        client.close()


def _apply_clarifier_result(
    controller_store: ControllerStore,
    session: Mapping[str, Any],
    result: TurnResult,
) -> None:
    payload = result.payload
    controller_store.append_planning_message(
        session_id=str(session["id"]),
        role=PlanningRole.CLARIFIER.value,
        text=str(payload["message"]),
        payload={"questions": payload["questions"]},
        raw_output=result.raw_output,
        model=result.model,
    )
    if payload["ready_to_summarize"]:
        controller_store.set_planning_understanding(
            session_id=str(session["id"]),
            summary=str(payload["understanding_summary"]),
        )
        transition_planning_session(
            controller_store,
            session_id=str(session["id"]),
            to_status=PlanningStatus.AWAITING_CONFIRMATION,
        )


def _apply_planner_result(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session: Mapping[str, Any],
    result: TurnResult,
) -> None:
    session_id = str(session["id"])
    payload = result.payload
    revision = int(session["plan_revision"]) + 1
    controller_store.record_plan_revision(
        session_id=session_id,
        revision=revision,
        plan_json=payload,
        plan_markdown=str(payload["plan_markdown"]),
    )
    controller_store.append_planning_message(
        session_id=session_id,
        role=PlanningRole.PLANNER.value,
        text=str(payload["plan_markdown"]),
        payload=payload,
        raw_output=result.raw_output,
        revision=revision,
        model=result.model,
    )
    # Every response here names a ledger finding: the turn validator rejects a
    # payload that names anything else, and the repair loop makes the planner
    # answer again.
    for response in payload.get("finding_responses", []):
        finding_id = str(response["finding_id"])
        controller_store.set_finding_response(
            session_id=session_id,
            finding_id=finding_id,
            status=str(response["status"]),
            planner_response=str(response["rationale"]),
        )
    transition_planning_session(
        controller_store,
        session_id=session_id,
        to_status=PlanningStatus.UNDER_REVIEW,
    )
    schedule_turn(controller_store, settings, session_id, TurnKind.REVIEWER)


def _apply_reviewer_result(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session: Mapping[str, Any],
    result: TurnResult,
) -> None:
    session_id = str(session["id"])
    revision_number = int(session["plan_revision"])
    round_number = int(session["review_turn"]) + 1
    ledger = _review_ledger(controller_store.planning_findings(session_id))
    known_ids = {str(finding["id"]) for finding in ledger}
    next_id = _next_finding_id(controller_store.planning_findings(session_id))
    normalised_findings: list[dict[str, str]] = []
    for finding in result.payload["findings"]:
        supplied_id = str(finding["id"])
        if supplied_id in known_ids:
            finding_id = supplied_id
        else:
            finding_id = f"F{next_id}"
            next_id += 1
        normalised = {
            "id": finding_id,
            "severity": str(finding["severity"]),
            "text": str(finding["text"]),
        }
        normalised_findings.append(normalised)
        controller_store.upsert_planning_finding(
            session_id=session_id,
            finding_id=finding_id,
            severity=normalised["severity"],
            text=normalised["text"],
            status=FindingStatus.OPEN.value,
            round_number=round_number,
        )
    controller_store.resolve_unseen_findings(session_id=session_id, round_number=round_number)
    persisted_round_findings = [
        finding
        for finding in controller_store.planning_findings(session_id)
        if int(finding["last_seen_round"]) == round_number
    ]
    has_blocking_finding = any(
        finding["severity"] in {"blocking", "major"}
        for finding in persisted_round_findings
    )
    approved = bool(result.payload["approved"]) and not has_blocking_finding
    controller_store.record_review_result(
        session_id=session_id,
        revision=revision_number,
        approved=approved,
        summary=str(result.payload["summary"]),
    )
    controller_store.append_planning_message(
        session_id=session_id,
        role=PlanningRole.REVIEWER.value,
        text=str(result.payload["summary"]),
        payload={"approved": approved, "findings": normalised_findings},
        raw_output=result.raw_output,
        revision=revision_number,
        model=result.model,
    )
    if bool(result.payload["approved"]) and has_blocking_finding:
        controller_store.append_planning_message(
            session_id=session_id,
            role=PlanningRole.SYSTEM.value,
            text="The reviewer verdict was overridden because it raised a blocking or major finding.",
        )

    updated_session = _required_session(controller_store, session_id)
    revision = _current_revision(controller_store, session_id, revision_number)
    if approved:
        _settle_with_plan_spec(
            controller_store,
            updated_session,
            revision,
            approved=True,
            status=PlanningStatus.PLAN_READY,
        )
    elif round_number >= int(updated_session["max_review_turns"]):
        _settle_with_plan_spec(
            controller_store,
            updated_session,
            revision,
            approved=False,
            status=PlanningStatus.REVIEW_LIMIT_REACHED,
        )
    else:
        transition_planning_session(
            controller_store,
            session_id=session_id,
            to_status=PlanningStatus.PLANNING,
        )
        schedule_turn(controller_store, settings, session_id, TurnKind.PLANNER)


def _record_turn_error(
    controller_store: ControllerStore,
    session_id: str,
    error: PlanningTurnError,
) -> None:
    session = _required_session(controller_store, session_id)
    if _is_terminal(session):
        _append_raw_system_message(controller_store, session_id, error.raw_output)
        return
    transition_planning_session(
        controller_store,
        session_id=session_id,
        to_status=PlanningStatus.FAILED,
        failure_reason=error.detail,
    )
    _append_raw_system_message(controller_store, session_id, error.raw_output)


def _append_raw_system_message(
    controller_store: ControllerStore,
    session_id: str,
    raw_output: str,
) -> None:
    controller_store.append_planning_message(
        session_id=session_id,
        role=PlanningRole.SYSTEM.value,
        text=raw_output,
        raw_output=raw_output,
    )


def _freeze_and_start_planning(
    controller_store: ControllerStore,
    settings: PlanningSettings,
    session: Mapping[str, Any],
    *,
    confirmed: bool,
) -> None:
    messages = controller_store.planning_messages(str(session["id"]))
    request = next((message["text"] for message in messages if message["role"] == "user"), "")
    brief = feature_brief(
        title=str(session["title"]),
        request=str(request),
        understanding=str(session["understanding_summary"]),
        messages=messages,
        confirmed=confirmed,
    )
    controller_store.freeze_planning_brief(
        session_id=str(session["id"]),
        brief=brief,
        confirmed=confirmed,
    )
    transition_planning_session(
        controller_store,
        session_id=str(session["id"]),
        to_status=PlanningStatus.PLANNING,
    )
    schedule_turn(controller_store, settings, str(session["id"]), TurnKind.PLANNER)


def _validate_clarifier_payload(payload: dict[str, Any]) -> list[str]:
    message = payload.get("message")
    questions = payload.get("questions")
    ready = payload.get("ready_to_summarize")
    summary = payload.get("understanding_summary")
    errors: list[str] = []
    if not isinstance(message, str):
        errors.append("clarifier message must be a string")
    if not isinstance(questions, list) or not all(isinstance(item, str) for item in questions):
        errors.append("clarifier questions must be a list of strings")
    if not isinstance(ready, bool):
        errors.append("clarifier ready_to_summarize must be a boolean")
    if not isinstance(summary, str):
        errors.append("clarifier understanding_summary must be a string")
    if errors:
        return errors
    if not message.strip():
        errors.append("clarifier message must be non-empty")
    if len(questions) > 3:
        errors.append("clarifier questions must contain at most three entries")
    if ready and (questions or not summary.strip()):
        errors.append("a ready clarifier response needs a summary and no questions")
    if not ready and not questions:
        errors.append("a clarifier response that is not ready needs questions")
    return errors


def _planner_validator(
    ledger_ids: set[str],
) -> Callable[[dict[str, Any]], list[str]]:
    """Builds the planner validator for one turn, bound to that turn's ledger.

    The ledger has to be part of validation rather than a filter applied after
    the fact. A planner that answers a finding nobody raised has misread its own
    task, and its plan is written against that misreading — so the turn is
    rejected and the repair loop makes it answer again, told exactly which ids
    exist. Round one binds an empty set, which rejects every response.
    """

    def validate(payload: dict[str, Any]) -> list[str]:
        errors = _validate_planner_payload(payload)
        if errors:
            return errors
        unknown = sorted(
            str(response["finding_id"])
            for response in payload.get("finding_responses", [])
            if str(response["finding_id"]) not in ledger_ids
        )
        if not unknown:
            return []
        known = ", ".join(sorted(ledger_ids)) if ledger_ids else "none"
        return [
            f"planner responded to findings that are not on the review ledger: "
            f"{', '.join(unknown)}. Respond only to these findings: {known}."
        ]

    return validate


def _validate_planner_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name in ("plan_markdown", "scope", "approach"):
        value = payload.get(name)
        if not isinstance(value, str):
            errors.append(f"planner {name} must be a string")
    components = payload.get("components")
    if not isinstance(components, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("responsibility", ""), str)
        for item in components
    ):
        errors.append("planner components must be a list of named components")
    risks = payload.get("risks")
    if not isinstance(risks, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("severity"), str)
        or not isinstance(item.get("text"), str)
        for item in risks
    ):
        errors.append("planner risks must be a list of valid risks")
    questions = payload.get("open_questions")
    if not isinstance(questions, list) or not all(isinstance(item, str) for item in questions):
        errors.append("planner open_questions must be a list of strings")
    # Absent is legal: the round-one schema omits the field, because there is no
    # ledger to respond to yet.
    responses = payload.get("finding_responses", [])
    if not isinstance(responses, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("finding_id"), str)
        or not isinstance(item.get("status"), str)
        or not isinstance(item.get("rationale"), str)
        for item in responses
    ):
        errors.append("planner finding_responses must be valid responses")
    if errors:
        return errors
    for name in ("plan_markdown", "scope", "approach"):
        if not payload[name].strip():
            errors.append(f"planner {name} must be non-empty")
    if any(not item["name"].strip() for item in components):
        errors.append("planner components must be a list of named components")
    if any(
        item["severity"] not in {"high", "medium", "low"} or not item["text"].strip()
        for item in risks
    ):
        errors.append("planner risks must be a list of valid risks")
    if any(
        item["status"] not in {FindingStatus.ANSWERED.value, FindingStatus.REJECTED.value}
        for item in responses
    ):
        errors.append("planner finding_responses must be valid responses")
    return errors


def _validate_reviewer_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload.get("approved"), bool):
        errors.append("reviewer approved must be a boolean")
    summary = payload.get("summary")
    if not isinstance(summary, str):
        errors.append("reviewer summary must be a string")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        errors.append("reviewer findings must be a list")
    else:
        for index, finding in enumerate(findings):
            label = f"reviewer findings[{index}]"
            if not isinstance(finding, dict):
                errors.append(f"{label} must be an object")
                continue
            if not isinstance(finding.get("id"), str):
                errors.append(f"{label}.id must be a string")
            if not isinstance(finding.get("severity"), str):
                errors.append(f"{label}.severity must be a string")
            if not isinstance(finding.get("text"), str):
                errors.append(f"{label}.text must be a string")
    if errors:
        return errors
    if not summary.strip():
        errors.append("reviewer summary must be non-empty")
    for index, finding in enumerate(findings):
        label = f"reviewer findings[{index}]"
        if not finding["id"].strip():
            errors.append(f"{label}.id must be non-empty")
        if finding["severity"] not in {"blocking", "major", "minor"}:
            errors.append(f"{label}.severity must be blocking, major, or minor")
        if not finding["text"].strip():
            errors.append(f"{label}.text must be non-empty")
    return errors


def _review_ledger(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(finding["finding_id"]),
            "severity": str(finding["severity"]),
            "text": str(finding["text"]),
            "status": str(finding["status"]),
            "planner_response": str(finding["planner_response"]),
            "raised_in_round": int(finding["raised_in_round"]),
        }
        for finding in findings
        if finding["status"] in _ACTIVE_FINDING_STATUSES
    ]


def _next_finding_id(findings: list[dict[str, Any]]) -> int:
    suffixes = [
        int(match.group(1))
        for finding in findings
        if (match := re.fullmatch(r"F(\d+)", str(finding["finding_id"])))
    ]
    return max(suffixes, default=0) + 1


def _current_revision(
    controller_store: ControllerStore,
    session_id: str,
    revision_number: int,
) -> dict[str, Any]:
    revision = next(
        (
            item
            for item in controller_store.plan_revisions(session_id)
            if int(item["revision"]) == revision_number
        ),
        None,
    )
    if revision is None:
        raise PlanningTurnError(500, f"planning revision {revision_number} is missing")
    return revision


def _settle_with_plan_spec(
    controller_store: ControllerStore,
    session: Mapping[str, Any],
    revision: Mapping[str, Any],
    *,
    approved: bool,
    status: PlanningStatus,
) -> None:
    plan_spec = build_plan_spec(
        session,
        revision,
        controller_store.planning_findings(str(session["id"])),
        approved,
    )
    controller_store.set_plan_spec(
        session_id=str(session["id"]),
        plan_spec=plan_spec.model_dump(mode="json"),
    )
    transition_planning_session(
        controller_store,
        session_id=str(session["id"]),
        to_status=status,
    )


def build_plan_spec(
    session: Mapping[str, Any],
    revision: Mapping[str, Any],
    findings: list[dict[str, Any]],
    approved: bool,
) -> PlanSpec:
    plan = _json_value(revision["plan_json"])
    if not isinstance(plan, dict):
        raise PlanningTurnError(500, "stored plan revision has invalid JSON")
    scope = str(plan["scope"])
    confirmed = bool(session["confirmed"])
    if not confirmed:
        scope = "The human chose to proceed without confirming a summary. " + scope
    outstanding = [] if approved else [
        _finding_data(finding)
        for finding in findings
        if finding["status"] != FindingStatus.RESOLVED.value
    ]
    return PlanSpec(
        title=str(session["title"]),
        scope=scope,
        approach=str(plan["approach"]),
        components=plan["components"],
        risks=plan["risks"],
        open_questions=plan["open_questions"],
        reviewer_outcome={
            "approved": approved,
            "rounds": int(session["review_turn"]),
            "summary": str(revision["reviewer_summary"]),
            "outstanding_findings": outstanding,
        },
        plan_markdown=str(plan["plan_markdown"]),
        confirmed_understanding=confirmed,
        generated_at=_generated_at(),
    )


def _finding_data(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": str(finding["finding_id"]),
        "severity": str(finding["severity"]),
        "text": str(finding["text"]),
        "status": str(finding["status"]),
        "planner_response": str(finding["planner_response"]),
        "raised_in_round": int(finding["raised_in_round"]),
        "last_seen_round": int(finding["last_seen_round"]),
    }


def _generated_at() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _session_for_project(
    controller_store: ControllerStore,
    project_name: str,
    session_id: str,
) -> dict[str, Any]:
    session = _required_session(controller_store, session_id)
    if str(session["project_name"]).casefold() != project_name.casefold():
        raise PlanningOperationError(404, "Planning session not found")
    return session


def _required_session(controller_store: ControllerStore, session_id: str) -> dict[str, Any]:
    session = controller_store.planning_session(session_id)
    if session is None:
        raise PlanningOperationError(404, "Planning session not found")
    return session


def _reject_terminal(session: Mapping[str, Any]) -> None:
    if _is_terminal(session):
        raise PlanningOperationError(409, "This planning session is terminal")


def _is_terminal(session: Mapping[str, Any]) -> bool:
    return PlanningStatus(str(session["status"])) in TERMINAL_PLANNING_STATUSES


def _session_model(session: Mapping[str, Any]) -> PlanningSession:
    return PlanningSession(**_session_data(session))


def _session_data(session: Mapping[str, Any]) -> dict[str, Any]:
    data = {
        key: value
        for key, value in session.items()
        if key
        in {
            "id",
            "project_id",
            "project_name",
            "sandbox_id",
            "title",
            "status",
            "turn_state",
            "clarifier_provider",
            "planner_provider",
            "reviewer_provider",
            "clarifier_model",
            "planner_model",
            "reviewer_model",
            "reviewer_reasoning_effort",
            "max_review_turns",
            "review_turn",
            "plan_revision",
            "confirmed",
            "understanding_summary",
            "failure_reason",
            "created_at",
            "updated_at",
            "settled_at",
        }
    }
    review_result = _json_object(session.get("review_result_json"))
    review_approved = review_result.get("approved")
    data["feature_status"] = derive_feature_status(
        PlanningStatus(str(session["status"])),
        context_status=_string_or_none(session.get("context_status")),
        delegation_status=_string_or_none(session.get("delegation_status")),
        review_status=_string_or_none(session.get("review_status")),
        review_approved=review_approved if isinstance(review_approved, bool) else None,
        source_merged_at=_string_or_none(session.get("review_source_merged_at")),
        change_status=_string_or_none(session.get("change_status")),
        pr_number=session.get("pr_number"),
        pr_state=_string_or_none(session.get("pr_state")),
        pr_merged_at=_string_or_none(session.get("pr_merged_at")),
    )
    return data


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _json_object(value: Any) -> dict[str, Any]:
    """Read a stored JSON object without trusting old rows to be valid JSON."""
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _message_model(row: Mapping[str, Any]) -> PlanningMessage:
    payload = _json_value(row.get("payload_json")) or {}
    if not isinstance(payload, dict):
        payload = {}
    approved = payload.get("approved")
    return PlanningMessage(
        sequence=row["sequence"],
        role=row["role"],
        text=row["text"],
        questions=payload.get("questions", []),
        revision=row["revision"],
        approved=approved if isinstance(approved, bool) else None,
        findings=[_message_finding(item) for item in payload.get("findings", [])],
        finding_responses=payload.get("finding_responses", []),
        has_raw_output=bool(row.get("raw_output")),
        model=str(row.get("model") or ""),
        created_at=row["created_at"],
    )


def _message_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Renames the reviewer payload's `id` to `finding_id`.

    The stored round payload uses `id`, because that is the key the reviewer
    prompt's schema asks for. Everything the UI reads calls it `finding_id`.
    """
    return {
        "finding_id": str(finding.get("id", "")),
        "severity": str(finding.get("severity", "")),
        "text": str(finding.get("text", "")),
    }


def _json_value(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value
