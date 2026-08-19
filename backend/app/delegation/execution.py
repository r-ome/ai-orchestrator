"""Run one work item and merge it internally after controller verification."""

import json
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

from app.coercions import json_object
from app.controller.store import ControllerStore, RevisionTaken, RunActive
from app.delegation import service
from app.delegation.config import get_routing_settings, get_verification_settings
from app.delegation.models import (
    DelegationStatus,
    FailureKind,
    RunStatus,
    StartRunOutcome,
    StartRunRequest,
    WorkItemState,
)
from app.delegation.packet import Packet, UpstreamResult, build_packet, render
from app.delegation.results import validate_result_payload
from app.delegation.routing import RoutingDecision, RoutingSettings, route
from app.delegation.verification import (
    VerificationOperationError,
    VerificationSettings,
    run_verification,
)
from app.implementation_context.models import ContextStatus
from app.implementation_context.service import ContextOperationError, get_context
from app.tasks.config import CodingTurnSettings
from app.tasks.models import RunTaskRequest, StartTaskRequest, TaskRunResponse
from app.tasks.runner import CodingTurnError
from app.tasks.service import (
    TaskOperationError,
    accept_task,
    get_task,
    reject_task,
    reopen_task_for_repair,
    run_task,
    start_task,
    verify_task,
)


def build_run_packet(
    store: ControllerStore,
    delegation_id: str,
    key: str,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> Packet:
    """Build a packet only from the revisions retained by this delegation."""
    delegation_view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    entry = _entry(delegation_view, key)
    session = store.planning_session(delegation_view.delegation.session_id)
    plan = json_object((session or {}).get("plan_spec_json"))
    context_id = delegation_view.delegation.context_id
    if not context_id:
        raise service.DelegationOperationError(
            409,
            "Delegation has no retained implementation context",
        )
    try:
        context = get_context(
            store,
            context_id,
            session_id=delegation_view.delegation.session_id,
            project_name=project_name,
        )
    except ContextOperationError as error:
        raise service.DelegationOperationError(
            error.status_code,
            error.detail,
        ) from error
    if context.status is not ContextStatus.READY:
        raise service.DelegationOperationError(
            409,
            f"Implementation context is '{context.status.value}', not ready",
        )

    return build_packet(
        item=entry.item,
        plan=plan or {},
        manifest=context.manifest.model_dump() if context.manifest else {},
        commands=context.confirmed_commands,
        upstream=_upstream(delegation_view, entry.item.dependencies),
    )


@dataclass(frozen=True)
class RunClaim:
    """A running work-item row plus what its turn needs. See ContextClaim.

    A coding turn waits up to `CODING_TURN_TIMEOUT_SECONDS` (1800 by default)
    and runs twice when the provider fails, so it cannot stay inside the
    request that asked for it.
    """

    run_id: str
    delegation_id: str
    key: str
    task_id: str
    sandbox_id: str
    packet: Packet
    decision: RoutingDecision
    turn_settings: CodingTurnSettings
    session_id: str | None
    project_name: str


@dataclass(frozen=True)
class Settlement:
    """The one way a run stage can finish.

    A failure kind requests task cleanup. A missing failure kind leaves an
    already-settled run alone, such as when another request settled it first.
    """

    failure_detail: str | None
    failure_kind: FailureKind | None
    halt: str | None
    changes: dict[str, Any]
    response: TaskRunResponse | None
    result_errors: list[str]
    raise_status: int | None = None
    raised_error: Exception | None = None
    include_cleanup_in_detail: bool = False
    append_cleanup_error: bool = False
    halt_uses_reported_detail: bool = False


@dataclass(frozen=True)
class CodingTurn:
    response: TaskRunResponse
    responses: list[TaskRunResponse]
    changes: dict[str, Any]
    result_errors: list[str]


@dataclass(frozen=True)
class VerificationTurn:
    coding: CodingTurn
    sandbox: dict[str, Any]
    settings: VerificationSettings
    verification: dict[str, Any]


@dataclass(frozen=True)
class MergeTurn:
    coding: CodingTurn
    verification: dict[str, Any]


def claim_run(
    docker_client: DockerClient,
    settings: CodingTurnSettings,
    store: ControllerStore,
    delegation_id: str,
    key: str,
    request: StartRunRequest,
    *,
    routing_settings: RoutingSettings | None = None,
    session_id: str | None = None,
    project_name: str | None = None,
) -> RunClaim:
    delegation_view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    entry = _entry(delegation_view, key)
    if delegation_view.delegation.status not in {
        DelegationStatus.READY,
        DelegationStatus.RUNNING,
    }:
        raise service.DelegationOperationError(
            409,
            "Delegation is "
            f"'{delegation_view.delegation.status.value}' and cannot run work",
        )
    if entry.state not in {WorkItemState.READY, WorkItemState.FAILED}:
        blocked = f", blocked by {entry.blocked_by}" if entry.blocked_by else ""
        raise service.DelegationOperationError(
            409,
            f"Work item '{key}' is '{entry.state.value}'{blocked}",
        )
    if not project_name:
        project_name = _project_name(store, delegation_view.delegation.sandbox_id)

    packet = build_run_packet(
        store,
        delegation_id,
        key,
        session_id=session_id,
        project_name=project_name,
    )
    if delegation_view.delegation.status is DelegationStatus.READY:
        service.transition(
            store,
            delegation_id,
            DelegationStatus.RUNNING,
            session_id=session_id,
            project_name=project_name,
        )

    try:
        task = start_task(
            docker_client,
            store,
            StartTaskRequest(
                project_name=project_name,
                title=entry.item.title[:200],
            ),
        )
    except TaskOperationError as error:
        raise service.DelegationOperationError(
            error.status_code,
            error.detail,
        ) from error

    override = store.work_item_routing(delegation_id).get(entry.item.id, {})
    decision = route(
        entry.item.complexity,
        routing_settings or get_routing_settings(),
        item_provider=_provider(override.get("provider")),
        item_model=override.get("model"),
        run_provider=request.provider,
        run_model=request.model,
    )
    run_id = uuid4().hex
    try:
        store.claim_work_item_run(
            {
                "id": run_id,
                "work_item_id": entry.item.id,
                "delegation_id": delegation_id,
                "status": RunStatus.RUNNING.value,
                "provider": decision.provider.value,
                "model": decision.model,
                "task_id": task.id,
            }
        )
        store.record_work_item_run(
            run_id,
            {"routing_source": decision.source.value},
        )
    except RevisionTaken as error:
        cleanup = _cleanup_task(docker_client, store, task.id)
        detail = "This work item run attempt was claimed concurrently"
        if cleanup:
            detail += f"; task cleanup failed: {cleanup}"
            service.transition(
                store,
                delegation_id,
                DelegationStatus.HALTED,
                error=detail[:1500],
                session_id=session_id,
                project_name=project_name,
            )
        raise service.DelegationOperationError(409, detail) from error
    except RunActive as error:
        cleanup = _cleanup_task(docker_client, store, task.id)
        detail = "Another work item run is already active"
        if cleanup:
            detail += f"; task cleanup failed: {cleanup}"
            service.transition(
                store,
                delegation_id,
                DelegationStatus.HALTED,
                error=detail[:1500],
                session_id=session_id,
                project_name=project_name,
            )
        raise service.DelegationOperationError(409, detail) from error

    _progress(
        store,
        run_id,
        delegation_view.delegation.sandbox_id,
        step="claimed",
        message=(
            f"Work item '{key}' reserved for "
            f"{decision.provider.value}/{decision.model}"
        ),
    )
    return RunClaim(
        run_id=run_id,
        delegation_id=delegation_id,
        key=key,
        task_id=task.id,
        sandbox_id=delegation_view.delegation.sandbox_id,
        packet=packet,
        decision=decision,
        turn_settings=replace(
            settings,
            credential_profile=request.credential_profile,
        ),
        session_id=session_id,
        project_name=project_name,
    )


def fail_run_claim(store: ControllerStore, claim: RunClaim, detail: str) -> None:
    """Settle a run whose turn never started. See jobs.submit_docker_job.

    The task branch is left alone: removing it needs the Docker client that
    could not be built. Startup reconciliation collects it instead.
    """
    store.settle_work_item_run(
        claim.run_id,
        to_status=RunStatus.FAILED.value,
        changes={"error": detail[:900], "failure_kind": FailureKind.UNKNOWN.value},
    )
    _progress(
        store,
        claim.run_id,
        claim.sandbox_id,
        step="failed",
        message=detail,
        level="error",
    )


def _progress(
    store: ControllerStore,
    run_id: str,
    sandbox_id: str,
    *,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    store.progress_event(
        sandbox_id=sandbox_id,
        run_id=run_id,
        kind="run.progress",
        step=step,
        message=message,
        level=level,
    )


def execute_run(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
    *,
    verification_settings: VerificationSettings | None = None,
) -> StartRunOutcome:
    """Run the turn behind a claim and settle its row.

    Safe to run off-request: every exit path settles `work_item_runs` and
    records a terminal `run.progress` event before returning or raising.
    """
    try:
        outcome = _execute_run(
            docker_client,
            store,
            claim,
            verification_settings=verification_settings,
        )
    except Exception as error:
        _progress(
            store,
            claim.run_id,
            claim.sandbox_id,
            step="failed",
            message=str(error) or "Work item run did not complete",
            level="error",
        )
        raise
    # Older rows can still be waiting for a decision. New successful runs
    # settle inside `_execute_run` after their verified commit is merged into
    # the internal sandbox branch.
    awaiting = outcome.run_status is RunStatus.RUNNING
    if awaiting:
        # Stamped before the event, so a crash between the two leaves a run
        # that reconciliation still treats as finished rather than one it
        # fails and strips the branch from.
        store.finish_work_item_turn(claim.run_id)
    _progress(
        store,
        claim.run_id,
        claim.sandbox_id,
        step="awaiting_decision" if awaiting else "settled",
        message=(
            "Turn finished; waiting for accept or reject"
            if awaiting
            else f"Run finished with status '{outcome.run_status.value}'"
        ),
        level="error" if outcome.run_status is RunStatus.FAILED else "info",
    )
    return outcome


def _execute_run(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
    *,
    verification_settings: VerificationSettings | None = None,
) -> StartRunOutcome:
    coding = _run_coding_turn(docker_client, store, claim)
    if isinstance(coding, Settlement):
        return _settle(docker_client, store, claim, coding)

    verification = _run_verification(
        docker_client,
        store,
        claim,
        coding,
        verification_settings=verification_settings,
    )
    if isinstance(verification, Settlement):
        return _settle(docker_client, store, claim, verification)

    merged = _repair_and_reverify(docker_client, store, claim, verification)
    if isinstance(merged, Settlement):
        return _settle(docker_client, store, claim, merged)

    return _settle(docker_client, store, claim, _merge(docker_client, store, claim, merged))


def _run_coding_turn(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
) -> CodingTurn | Settlement:
    _progress(
        store,
        claim.run_id,
        claim.sandbox_id,
        step="turn",
        message=(
            "Running the coding turn on "
            f"{claim.decision.provider.value}/{claim.decision.model}"
        ),
    )
    responses: list[TaskRunResponse] = []
    try:
        response = run_task(
            docker_client,
            store,
            claim.turn_settings,
            claim.task_id,
            RunTaskRequest(
                prompt=render(claim.packet),
                provider=claim.decision.provider,
                model=claim.decision.model,
            ),
        )
        responses.append(response)
        if not response.committed and _failure_kind(response) is FailureKind.PROVIDER:
            response = run_task(
                docker_client,
                store,
                claim.turn_settings,
                claim.task_id,
                RunTaskRequest(
                    prompt=render(claim.packet),
                    provider=claim.decision.provider,
                    model=claim.decision.model,
                ),
            )
            responses.append(response)
    except (TaskOperationError, CodingTurnError) as error:
        first_response_was_provider_failure = (
            bool(responses)
            and _failure_kind(responses[0]) is FailureKind.PROVIDER
        )
        return Settlement(
            failure_detail=error.detail,
            failure_kind=(
                FailureKind.PROVIDER
                if isinstance(error, CodingTurnError)
                else FailureKind.IMPLEMENTATION
            ),
            halt=(
                "Provider failed twice for one work item"
                if first_response_was_provider_failure
                else None
            ),
            changes={},
            response=None,
            result_errors=[],
            raise_status=error.status_code,
            include_cleanup_in_detail=True,
        )
    except Exception as error:
        return Settlement(
            failure_detail=str(error) or type(error).__name__,
            failure_kind=FailureKind.UNKNOWN,
            halt=None,
            changes={},
            response=None,
            result_errors=[],
            raised_error=error,
        )

    changes = _metrics(responses)
    result, result_errors = _result(response)
    if result is not None:
        changes["result_json"] = json.dumps(result)
    if response.committed:
        return CodingTurn(response, responses, changes, result_errors)

    reason = response.turn_error or response.detail or "Turn did not commit changes"
    exhausted_provider_retry_returned_uncommitted = (
        len(responses) == 2 and _failure_kind(response) is FailureKind.PROVIDER
    )
    return Settlement(
        failure_detail=reason,
        failure_kind=_failure_kind(response),
        halt=(
            "Provider failed twice for one work item"
            if exhausted_provider_retry_returned_uncommitted
            else None
        ),
        changes=changes,
        response=response,
        result_errors=result_errors,
        append_cleanup_error=True,
    )


def _run_verification(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
    coding: CodingTurn,
    *,
    verification_settings: VerificationSettings | None,
) -> VerificationTurn | Settlement:
    sandbox = store.sandbox(claim.sandbox_id)
    if sandbox is None:
        return Settlement(
            failure_detail="Delegation sandbox was not found",
            failure_kind=FailureKind.UNKNOWN,
            halt=None,
            changes=coding.changes,
            response=coding.response,
            result_errors=coding.result_errors,
            raise_status=404,
        )
    settings = verification_settings or get_verification_settings()
    try:
        first_verification = run_verification(
            docker_client,
            settings,
            volume_name=str(sandbox["volume_name"]),
            commands=claim.packet.verification,
            controller_store=store,
            sandbox_id=claim.sandbox_id,
        )
    except VerificationOperationError as error:
        return Settlement(
            failure_detail=error.detail,
            failure_kind=FailureKind.VERIFICATION,
            halt=error.detail,
            changes=coding.changes,
            response=coding.response,
            result_errors=coding.result_errors,
            raise_status=error.status_code,
        )

    verification = {
        "passed": first_verification["passed"],
        "repair_count": 0,
        "attempts": [first_verification],
    }
    coding.changes["verification_json"] = json.dumps(verification)
    return VerificationTurn(coding, sandbox, settings, verification)


def _repair_and_reverify(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
    verification_turn: VerificationTurn,
) -> MergeTurn | Settlement:
    coding = verification_turn.coding
    verification = verification_turn.verification
    if verification["passed"]:
        return MergeTurn(coding, verification)

    first_verification = verification["attempts"][0]
    previous_head = coding.response.task.head_commit
    try:
        reopen_task_for_repair(store, claim.task_id)
        repair_response = run_task(
            docker_client,
            store,
            claim.turn_settings,
            claim.task_id,
            RunTaskRequest(
                prompt=_verification_repair_prompt(claim.packet, first_verification),
                provider=claim.decision.provider,
                model=claim.decision.model,
            ),
        )
    except (TaskOperationError, CodingTurnError) as error:
        coding.changes["repair_count"] = 1
        return Settlement(
            failure_detail=error.detail,
            failure_kind=FailureKind.VERIFICATION,
            halt=error.detail,
            changes=coding.changes,
            response=coding.response,
            result_errors=coding.result_errors,
            raise_status=error.status_code,
        )

    coding.responses.append(repair_response)
    coding.changes.update(_metrics(coding.responses))
    coding.changes["repair_count"] = 1
    repair_result, repair_errors = _result(repair_response)
    coding.result_errors.extend(repair_errors)
    if repair_result is not None:
        coding.changes["result_json"] = json.dumps(repair_result)
    no_new_commit = repair_response.task.head_commit == previous_head
    if not repair_response.committed or no_new_commit:
        reason = (
            # Name what the repair was sent to fix. On its own "did not
            # create a new commit" reads as the repair misbehaving, when
            # the usual cause is a verification failure the model could not
            # act on — a sandbox fault, or a command that was never going
            # to pass. Without this the real error is only in the run row.
            "Focused repair made no commit; the verification failure it was "
            f"sent to fix was: {_verification_failure(first_verification)}"
            if no_new_commit
            else repair_response.turn_error
            or repair_response.detail
            or "Focused repair failed"
        )
        return Settlement(
            failure_detail=reason,
            failure_kind=FailureKind.VERIFICATION,
            halt=reason,
            changes=coding.changes,
            response=repair_response,
            result_errors=coding.result_errors,
        )

    try:
        second_verification = run_verification(
            docker_client,
            verification_turn.settings,
            volume_name=str(verification_turn.sandbox["volume_name"]),
            commands=claim.packet.verification,
            controller_store=store,
            sandbox_id=claim.sandbox_id,
        )
    except VerificationOperationError as error:
        second_verification = {
            "passed": False,
            "commands": [],
            "error": error.detail,
        }
    verification = {
        "passed": second_verification["passed"],
        "repair_count": 1,
        "attempts": [first_verification, second_verification],
    }
    coding.changes["verification_json"] = json.dumps(verification)
    if not second_verification["passed"]:
        reason = _verification_failure(second_verification)
        return Settlement(
            failure_detail=reason,
            failure_kind=FailureKind.VERIFICATION,
            halt=reason,
            changes=coding.changes,
            response=repair_response,
            result_errors=coding.result_errors,
        )
    return MergeTurn(
        CodingTurn(
            repair_response,
            coding.responses,
            coding.changes,
            coding.result_errors,
        ),
        verification,
    )


def _merge(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
    merge_turn: MergeTurn,
) -> Settlement:
    coding = merge_turn.coding
    try:
        verify_task(
            store,
            claim.task_id,
            verification_passed=bool(merge_turn.verification["passed"]),
            detail="Controller-run verification passed",
        )
    except TaskOperationError as error:
        return Settlement(
            failure_detail=error.detail,
            failure_kind=FailureKind.VERIFICATION,
            halt=None,
            changes=coding.changes,
            response=coding.response,
            result_errors=coding.result_errors,
            raise_status=error.status_code,
            include_cleanup_in_detail=True,
        )

    store.record_work_item_run(claim.run_id, coding.changes)
    try:
        accepted = accept_task(docker_client, store, claim.task_id)
    except TaskOperationError as error:
        return Settlement(
            failure_detail=error.detail,
            failure_kind=FailureKind.IMPLEMENTATION,
            halt=error.detail,
            changes=coding.changes,
            response=coding.response,
            result_errors=coding.result_errors,
            raise_status=error.status_code,
            include_cleanup_in_detail=True,
            halt_uses_reported_detail=True,
        )

    if store.settle_work_item_run(
        claim.run_id,
        to_status=RunStatus.SUCCEEDED.value,
        changes=coding.changes,
    ) is None:
        # Another request owns this run after it has settled it.
        return Settlement(
            failure_detail="Run was settled by another request",
            failure_kind=None,
            halt=None,
            changes=coding.changes,
            response=coding.response,
            result_errors=coding.result_errors,
            raise_status=409,
        )
    _complete_if_finished(
        store,
        claim.delegation_id,
        session_id=claim.session_id,
        project_name=claim.project_name,
    )
    return Settlement(
        failure_detail=None,
        failure_kind=None,
        halt=None,
        changes=coding.changes,
        response=coding.response.model_copy(update={"task": accepted}),
        result_errors=coding.result_errors,
    )


def _settle(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: RunClaim,
    settlement: Settlement,
) -> StartRunOutcome:
    detail = settlement.failure_detail
    if settlement.failure_kind is not None:
        assert detail is not None
        cleanup = _fail_run_and_cleanup(
            docker_client,
            store,
            claim.delegation_id,
            claim.run_id,
            claim.task_id,
            detail,
            settlement.failure_kind,
            changes=settlement.changes,
            session_id=claim.session_id,
            project_name=claim.project_name,
        )
        if cleanup and settlement.append_cleanup_error:
            settlement.result_errors.append(f"task cleanup failed: {cleanup}")
        if cleanup and settlement.include_cleanup_in_detail:
            detail += f"; task cleanup failed: {cleanup}"
        if settlement.halt is not None:
            _halt_delegation(
                store,
                claim.delegation_id,
                detail if settlement.halt_uses_reported_detail else settlement.halt,
                session_id=claim.session_id,
                project_name=claim.project_name,
            )

    if settlement.raised_error is not None:
        raise settlement.raised_error
    if settlement.raise_status is not None:
        assert detail is not None
        raise service.DelegationOperationError(settlement.raise_status, detail)
    return _outcome(
        store,
        claim.delegation_id,
        claim.run_id,
        packet=claim.packet,
        result_errors=settlement.result_errors,
        response=settlement.response,
        decision=claim.decision,
        session_id=claim.session_id,
        project_name=claim.project_name,
    )


def start_run(
    docker_client: DockerClient,
    settings: CodingTurnSettings,
    store: ControllerStore,
    delegation_id: str,
    key: str,
    request: StartRunRequest,
    *,
    routing_settings: RoutingSettings | None = None,
    verification_settings: VerificationSettings | None = None,
    session_id: str | None = None,
    project_name: str | None = None,
) -> StartRunOutcome:
    """Claim and run one work item, start to finish."""
    claim = claim_run(
        docker_client,
        settings,
        store,
        delegation_id,
        key,
        request,
        routing_settings=routing_settings,
        session_id=session_id,
        project_name=project_name,
    )
    return execute_run(
        docker_client,
        store,
        claim,
        verification_settings=verification_settings,
    )


def accept_run(
    docker_client: DockerClient,
    store: ControllerStore,
    delegation_id: str,
    run_id: str,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> StartRunOutcome:
    run = _scoped_run(
        store,
        delegation_id,
        run_id,
        session_id=session_id,
        project_name=project_name,
    )
    _require_running(run)
    task_id = str(run.get("task_id") or "")
    if not task_id:
        raise service.DelegationOperationError(409, "Run has no task to accept")
    try:
        accept_task(docker_client, store, task_id)
    except TaskOperationError as error:
        raise service.DelegationOperationError(
            error.status_code,
            error.detail,
        ) from error

    if store.settle_work_item_run(run_id, to_status=RunStatus.SUCCEEDED.value) is None:
        raise service.DelegationOperationError(409, "Run was settled by another request")
    _complete_if_finished(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    return _outcome(
        store,
        delegation_id,
        run_id,
        task_id=task_id,
        session_id=session_id,
        project_name=project_name,
    )


def reject_run(
    docker_client: DockerClient,
    store: ControllerStore,
    delegation_id: str,
    run_id: str,
    reason: str = "",
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> StartRunOutcome:
    run = _scoped_run(
        store,
        delegation_id,
        run_id,
        session_id=session_id,
        project_name=project_name,
    )
    _require_running(run)
    task_id = str(run.get("task_id") or "")
    if task_id:
        try:
            reject_task(docker_client, store, task_id)
        except TaskOperationError as error:
            raise service.DelegationOperationError(
                error.status_code,
                error.detail,
            ) from error
    if store.settle_work_item_run(
        run_id,
        to_status=RunStatus.FAILED.value,
        changes={
            "failure_kind": FailureKind.IMPLEMENTATION.value,
            "error": reason or "rejected by a person",
        },
    ) is None:
        raise service.DelegationOperationError(409, "Run was settled by another request")
    return _outcome(
        store,
        delegation_id,
        run_id,
        task_id=task_id or None,
        session_id=session_id,
        project_name=project_name,
    )


def _complete_if_finished(
    store: ControllerStore,
    delegation_id: str,
    *,
    session_id: str | None,
    project_name: str | None,
) -> None:
    delegation_view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    if delegation_view.items and all(
        entry.state is WorkItemState.COMPLETED for entry in delegation_view.items
    ):
        service.transition(
            store,
            delegation_id,
            DelegationStatus.COMPLETED,
            session_id=session_id,
            project_name=project_name,
        )


def _upstream(delegation_view: Any, dependencies: list[str]) -> list[UpstreamResult]:
    wanted = set(dependencies)
    results: list[UpstreamResult] = []
    for entry in delegation_view.items:
        if entry.item.key not in wanted:
            continue
        succeeded = next(
            (run for run in entry.runs if run.status is RunStatus.SUCCEEDED),
            None,
        )
        if succeeded is None or not succeeded.result:
            continue
        results.append(
            UpstreamResult(
                key=entry.item.key,
                title=entry.item.title,
                changed=_strings(succeeded.result.get("changed")),
                interfaces=_strings(succeeded.result.get("interfaces")),
                notes=_strings(succeeded.result.get("notes_for_downstream")),
            )
        )
    return results


def _result(response: TaskRunResponse) -> tuple[dict[str, Any] | None, list[str]]:
    payload = response.result
    if not isinstance(payload, dict):
        return None, ["the run reported no JSON result object"]
    return payload, validate_result_payload(payload)


def _metrics(responses: list[TaskRunResponse]) -> dict[str, Any]:
    response = responses[-1]
    return {
        "model": response.model,
        "input_tokens": _sum(
            [entry.usage.input_tokens for entry in responses]
        ),
        "output_tokens": _sum(
            [entry.usage.output_tokens for entry in responses]
        ),
        "cache_read_tokens": _sum(
            [entry.usage.cache_read_tokens for entry in responses]
        ),
        "cache_creation_tokens": _sum(
            [entry.usage.cache_creation_tokens for entry in responses]
        ),
        "cost_usd": _sum_float([entry.usage.cost_usd for entry in responses]),
        "duration_ms": _sum([entry.duration_ms for entry in responses]),
        "exit_code": response.exit_code,
    }


def _failure_kind(response: TaskRunResponse) -> FailureKind:
    if response.turn_status in {"provider_failure", "timed_out", "tool_failure"}:
        return FailureKind.PROVIDER
    return FailureKind.IMPLEMENTATION


def _fail_run_and_cleanup(
    docker_client: DockerClient,
    store: ControllerStore,
    delegation_id: str,
    run_id: str,
    task_id: str,
    error: str,
    failure_kind: FailureKind,
    *,
    changes: dict[str, Any] | None = None,
    session_id: str | None,
    project_name: str | None,
) -> str:
    cleanup = _cleanup_task(docker_client, store, task_id)
    detail = error + (f"; task cleanup failed: {cleanup}" if cleanup else "")
    store.settle_work_item_run(
        run_id,
        to_status=RunStatus.FAILED.value,
        changes={
            **(changes or {}),
            "failure_kind": failure_kind.value,
            "error": detail[:2000],
        },
    )
    if cleanup:
        try:
            service.transition(
                store,
                delegation_id,
                DelegationStatus.HALTED,
                error=detail[:1500],
                session_id=session_id,
                project_name=project_name,
            )
        except service.DelegationOperationError:
            pass
    return cleanup


def _halt_delegation(
    store: ControllerStore,
    delegation_id: str,
    reason: str,
    *,
    session_id: str | None,
    project_name: str | None,
) -> None:
    try:
        service.transition(
            store,
            delegation_id,
            DelegationStatus.HALTED,
            error=reason[:1500],
            session_id=session_id,
            project_name=project_name,
        )
    except service.DelegationOperationError:
        pass


def _cleanup_task(
    docker_client: DockerClient,
    store: ControllerStore,
    task_id: str,
) -> str:
    try:
        reject_task(docker_client, store, task_id)
    except Exception as error:  # noqa: BLE001 - cleanup failure must be retained
        return str(getattr(error, "detail", error))
    return ""


def _outcome(
    store: ControllerStore,
    delegation_id: str,
    run_id: str,
    *,
    packet: Packet | None = None,
    result_errors: list[str] | None = None,
    response: TaskRunResponse | None = None,
    task_id: str | None = None,
    decision: RoutingDecision | None = None,
    session_id: str | None,
    project_name: str | None,
) -> StartRunOutcome:
    delegation_view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    run = _run(store, run_id)
    resolved_task_id = task_id or run.get("task_id")
    task_status = None
    if resolved_task_id:
        task_status = get_task(store, str(resolved_task_id)).status.value
    return StartRunOutcome(
        delegation=delegation_view,
        run_id=run_id,
        run_status=RunStatus(str(run["status"])),
        task_id=str(resolved_task_id) if resolved_task_id else None,
        task_status=task_status,
        turn_status=response.turn_status if response else None,
        committed=response.committed if response else None,
        result_errors=result_errors or [],
        packet=packet,
        model=str(run.get("model") or ""),
        routing_source=run.get("routing_source"),
        recommended_model=decision.recommended_model if decision else None,
        routing_warning=decision.warning if decision else None,
    )


def _entry(delegation_view: Any, key: str) -> Any:
    for entry in delegation_view.items:
        if entry.item.key == key:
            return entry
    raise service.DelegationOperationError(404, f"Work item '{key}' was not found")


def _run(store: ControllerStore, run_id: str) -> dict[str, Any]:
    row = store.work_item_run(run_id)
    if row is None:
        raise service.DelegationOperationError(404, f"Run '{run_id}' was not found")
    return row


def _scoped_run(
    store: ControllerStore,
    delegation_id: str,
    run_id: str,
    *,
    session_id: str | None,
    project_name: str | None,
) -> dict[str, Any]:
    service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    row = _run(store, run_id)
    if str(row["delegation_id"]) != delegation_id:
        raise service.DelegationOperationError(404, f"Run '{run_id}' was not found")
    return row


def _require_running(run: dict[str, Any]) -> None:
    if str(run["status"]) != RunStatus.RUNNING.value:
        raise service.DelegationOperationError(
            409,
            f"Run is already settled as '{run['status']}'",
        )


def _project_name(store: ControllerStore, sandbox_id: str) -> str:
    sandbox = store.sandbox(sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(
            404,
            f"Sandbox '{sandbox_id}' was not found",
        )
    return str(sandbox["project_name"])


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry.strip()]


def _verification_repair_prompt(packet: Packet, verification: dict[str, Any]) -> str:
    failures = [
        command
        for command in verification.get("commands", [])
        if not command.get("passed")
    ]
    detail = "\n\n".join(
        f"Command: {failure.get('command')}\n"
        f"Result: {failure.get('detail')}\n"
        f"Output:\n{str(failure.get('output') or '')[-8000:]}"
        for failure in failures
    )
    return (
        f"{render(packet)}\n\n"
        "## Focused repair\n\n"
        "The implementation committed, but controller verification failed. "
        "Fix only these failures and keep the original scope.\n\n"
        f"{detail or 'Verification produced no command detail.'}"
    )


def _verification_failure(verification: dict[str, Any]) -> str:
    if verification.get("error"):
        return str(verification["error"])
    failed = next(
        (
            command
            for command in verification.get("commands", [])
            if not command.get("passed")
        ),
        None,
    )
    if failed:
        return (
            f"Verification failed: {failed.get('command')} "
            f"({failed.get('detail')})"
        )
    return "Verification failed without command detail"


def _sum(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _sum_float(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _provider(value: Any) -> Any:
    from app.agents.models import AgentProvider

    try:
        return AgentProvider(str(value)) if value else None
    except ValueError:
        return None


__all__ = ["accept_run", "build_run_packet", "reject_run", "start_run"]
