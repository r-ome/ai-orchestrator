"""Run one work item and merge it internally after controller verification."""

import json
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

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
    plan = _object((session or {}).get("plan_spec_json"))
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
    store.event(
        sandbox_id=sandbox_id,
        run_id=run_id,
        kind="run.progress",
        payload={"step": step, "message": message[:900], "level": level},
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
    delegation_id = claim.delegation_id
    run_id = claim.run_id
    task_id = claim.task_id
    packet = claim.packet
    decision = claim.decision
    effective_settings = claim.turn_settings
    session_id = claim.session_id
    project_name = claim.project_name

    _progress(
        store,
        run_id,
        claim.sandbox_id,
        step="turn",
        message=f"Running the coding turn on {decision.provider.value}/{decision.model}",
    )
    responses: list[TaskRunResponse] = []
    try:
        response = run_task(
            docker_client,
            store,
            effective_settings,
            task_id,
            RunTaskRequest(
                prompt=render(packet),
                provider=decision.provider,
                model=decision.model,
            ),
        )
        responses.append(response)
        if not response.committed and _failure_kind(response) is FailureKind.PROVIDER:
            response = run_task(
                docker_client,
                store,
                effective_settings,
                task_id,
                RunTaskRequest(
                    prompt=render(packet),
                    provider=decision.provider,
                    model=decision.model,
                ),
            )
            responses.append(response)
    except (TaskOperationError, CodingTurnError) as error:
        cleanup = _fail_run_and_cleanup(
            docker_client,
            store,
            delegation_id,
            run_id,
            task_id,
            error.detail,
            FailureKind.PROVIDER
            if isinstance(error, CodingTurnError)
            else FailureKind.IMPLEMENTATION,
            session_id=session_id,
            project_name=project_name,
        )
        detail = error.detail + (f"; task cleanup failed: {cleanup}" if cleanup else "")
        if responses and _failure_kind(responses[0]) is FailureKind.PROVIDER:
            _halt_delegation(
                store,
                delegation_id,
                "Provider failed twice for one work item",
                session_id=session_id,
                project_name=project_name,
            )
        raise service.DelegationOperationError(error.status_code, detail) from error
    except Exception as error:
        _fail_run_and_cleanup(
            docker_client,
            store,
            delegation_id,
            run_id,
            task_id,
            str(error) or type(error).__name__,
            FailureKind.UNKNOWN,
            session_id=session_id,
            project_name=project_name,
        )
        raise

    changes = _metrics(responses)
    result, result_errors = _result(response)
    if result is not None:
        changes["result_json"] = json.dumps(result)

    if not response.committed:
        reason = response.turn_error or response.detail or "Turn did not commit changes"
        cleanup = _fail_run_and_cleanup(
            docker_client,
            store,
            delegation_id,
            run_id,
            task_id,
            reason,
            _failure_kind(response),
            changes=changes,
            session_id=session_id,
            project_name=project_name,
        )
        if cleanup:
            result_errors.append(f"task cleanup failed: {cleanup}")
        if (
            len(responses) == 2
            and _failure_kind(response) is FailureKind.PROVIDER
        ):
            _halt_delegation(
                store,
                delegation_id,
                "Provider failed twice for one work item",
                session_id=session_id,
                project_name=project_name,
            )
        return _outcome(
            store,
            delegation_id,
            run_id,
            packet=packet,
            result_errors=result_errors,
            response=response,
            decision=decision,
            session_id=session_id,
            project_name=project_name,
        )

    sandbox = store.sandbox(claim.sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(404, "Delegation sandbox was not found")
    verifier_settings = verification_settings or get_verification_settings()
    try:
        first_verification = run_verification(
            docker_client,
            verifier_settings,
            volume_name=str(sandbox["volume_name"]),
            commands=packet.verification,
            controller_store=store,
            sandbox_id=claim.sandbox_id,
        )
    except VerificationOperationError as error:
        _fail_run_and_cleanup(
            docker_client,
            store,
            delegation_id,
            run_id,
            task_id,
            error.detail,
            FailureKind.VERIFICATION,
            changes=changes,
            session_id=session_id,
            project_name=project_name,
        )
        _halt_delegation(
            store,
            delegation_id,
            error.detail,
            session_id=session_id,
            project_name=project_name,
        )
        raise service.DelegationOperationError(
            error.status_code,
            error.detail,
        ) from error

    verification = {
        "passed": first_verification["passed"],
        "repair_count": 0,
        "attempts": [first_verification],
    }
    changes["verification_json"] = json.dumps(verification)

    if not first_verification["passed"]:
        previous_head = response.task.head_commit
        try:
            reopen_task_for_repair(store, task_id)
            repair_response = run_task(
                docker_client,
                store,
                effective_settings,
                task_id,
                RunTaskRequest(
                    prompt=_verification_repair_prompt(packet, first_verification),
                    provider=decision.provider,
                    model=decision.model,
                ),
            )
        except (TaskOperationError, CodingTurnError) as error:
            changes["repair_count"] = 1
            _fail_run_and_cleanup(
                docker_client,
                store,
                delegation_id,
                run_id,
                task_id,
                error.detail,
                FailureKind.VERIFICATION,
                changes=changes,
                session_id=session_id,
                project_name=project_name,
            )
            _halt_delegation(
                store,
                delegation_id,
                error.detail,
                session_id=session_id,
                project_name=project_name,
            )
            raise service.DelegationOperationError(
                error.status_code,
                error.detail,
            ) from error

        responses.append(repair_response)
        changes.update(_metrics(responses))
        changes["repair_count"] = 1
        repair_result, repair_errors = _result(repair_response)
        result_errors.extend(repair_errors)
        if repair_result is not None:
            changes["result_json"] = json.dumps(repair_result)
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
            _fail_run_and_cleanup(
                docker_client,
                store,
                delegation_id,
                run_id,
                task_id,
                reason,
                FailureKind.VERIFICATION,
                changes=changes,
                session_id=session_id,
                project_name=project_name,
            )
            _halt_delegation(
                store,
                delegation_id,
                reason,
                session_id=session_id,
                project_name=project_name,
            )
            return _outcome(
                store,
                delegation_id,
                run_id,
                packet=packet,
                result_errors=result_errors,
                response=repair_response,
                decision=decision,
                session_id=session_id,
                project_name=project_name,
            )

        response = repair_response
        try:
            second_verification = run_verification(
                docker_client,
                verifier_settings,
                volume_name=str(sandbox["volume_name"]),
                commands=packet.verification,
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
        changes["verification_json"] = json.dumps(verification)
        if not second_verification["passed"]:
            reason = _verification_failure(second_verification)
            _fail_run_and_cleanup(
                docker_client,
                store,
                delegation_id,
                run_id,
                task_id,
                reason,
                FailureKind.VERIFICATION,
                changes=changes,
                session_id=session_id,
                project_name=project_name,
            )
            _halt_delegation(
                store,
                delegation_id,
                reason,
                session_id=session_id,
                project_name=project_name,
            )
            return _outcome(
                store,
                delegation_id,
                run_id,
                packet=packet,
                result_errors=result_errors,
                response=response,
                decision=decision,
                session_id=session_id,
                project_name=project_name,
            )

    try:
        verified = verify_task(
            store,
            task_id,
            verification_passed=bool(verification["passed"]),
            detail="Controller-run verification passed",
        )
    except TaskOperationError as error:
        cleanup = _fail_run_and_cleanup(
            docker_client,
            store,
            delegation_id,
            run_id,
            task_id,
            error.detail,
            FailureKind.VERIFICATION,
            changes=changes,
            session_id=session_id,
            project_name=project_name,
        )
        detail = error.detail + (f"; task cleanup failed: {cleanup}" if cleanup else "")
        raise service.DelegationOperationError(error.status_code, detail) from error

    store.record_work_item_run(run_id, changes)
    try:
        accepted = accept_task(docker_client, store, task_id)
    except TaskOperationError as error:
        cleanup = _fail_run_and_cleanup(
            docker_client,
            store,
            delegation_id,
            run_id,
            task_id,
            error.detail,
            FailureKind.IMPLEMENTATION,
            changes=changes,
            session_id=session_id,
            project_name=project_name,
        )
        detail = error.detail + (f"; task cleanup failed: {cleanup}" if cleanup else "")
        _halt_delegation(
            store,
            delegation_id,
            detail,
            session_id=session_id,
            project_name=project_name,
        )
        raise service.DelegationOperationError(error.status_code, detail) from error

    if store.settle_work_item_run(
        run_id,
        to_status=RunStatus.SUCCEEDED.value,
        changes=changes,
    ) is None:
        raise service.DelegationOperationError(409, "Run was settled by another request")
    _complete_if_finished(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    response = response.model_copy(update={"task": accepted})
    return _outcome(
        store,
        delegation_id,
        run_id,
        packet=packet,
        result_errors=result_errors,
        response=response,
        decision=decision,
        session_id=session_id,
        project_name=project_name,
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


def _object(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["accept_run", "build_run_packet", "reject_run", "start_run"]
