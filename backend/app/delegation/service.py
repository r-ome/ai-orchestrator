import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

from app.agents.models import AgentProvider
from app.controller.store import (
    ControllerStore,
    DelegationActive,
    RevisionTaken,
    SandboxWriterAdmissionError,
)
from app.delegation import graph, prompts
from app.delegation.config import (
    DelegatorSettings,
    get_routing_settings,
)
from app.delegation.models import (
    ACTIVE_DELEGATION_STATUSES,
    TERMINAL_DELEGATION_STATUSES,
    ChangeRequestStatus,
    Delegation,
    DelegationStatus,
    DelegationView,
    FeatureChangeRequest,
    GenerateDelegationOutcome,
    GenerateDelegationRequest,
    IntegrationReview,
    IntegrationReviewStatus,
    ItemRouting,
    RunStatus,
    RunUsage,
    VerificationIntent,
    WorkItem,
    WorkItemRun,
    WorkItemView,
    delegation_source_statuses,
)
from app.delegation.routing import route
from app.implementation_context.service import ready_context
from app.planning.config import PlanningSettings
from app.planning.models import PlanningRole, PlanningStatus
from app.planning.runner import (
    TurnRequest,
    run_planning_turn,
    run_validated_turn,
)
from app.platform.coercions import json_object
from app.platform.errors import OperationError

_DELEGATABLE_SESSION_STATUSES = {
    PlanningStatus.PLAN_READY.value,
    PlanningStatus.REVIEW_LIMIT_REACHED.value,
}
_LIST_COLUMNS = {
    "dependencies": "dependencies_json",
    "files": "files_json",
    "symbols": "symbols_json",
    "write_scope": "write_scope_json",
    "acceptance_criteria": "acceptance_criteria_json",
    "architecture": "architecture_json",
    "risks": "risks_json",
}


class DelegationOperationError(OperationError):
    """A delegation operation failed."""


def confirmed_command_kinds(
    store: ControllerStore,
    session_id: str,
) -> frozenset[str]:
    context = ready_context(store, session_id)
    if context is None:
        return frozenset()
    return frozenset(context.confirmed_commands)


def create_revision(
    store: ControllerStore,
    session_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    project_name: str | None = None,
) -> DelegationView:
    """Validate and retain the next immutable decomposition revision."""
    session = _delegatable_session(store, session_id, project_name=project_name)
    context = ready_context(store, session_id)
    errors = graph.validate_work_items(
        items,
        available_command_kinds=(
            frozenset(context.confirmed_commands) if context is not None else None
        ),
    )
    if errors:
        raise DelegationOperationError(422, "; ".join(errors)[:1500])

    delegation_id = uuid4().hex
    rows = [
        _work_item_row(delegation_id, position, item)
        for position, item in enumerate(items)
    ]
    try:
        store.claim_delegation_revision(
            {
                "id": delegation_id,
                "session_id": session_id,
                "sandbox_id": str(session["sandbox_id"]),
                "context_id": context.id if context else None,
                "status": DelegationStatus.READY.value,
            },
            rows,
        )
    except SandboxWriterAdmissionError as error:
        raise DelegationOperationError(409, str(error)) from error
    except RevisionTaken as error:
        raise DelegationOperationError(
            409,
            "This delegation revision was claimed concurrently",
        ) from error
    except DelegationActive as error:
        raise DelegationOperationError(
            409,
            "This sandbox already has an active delegation",
        ) from error
    return view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )


@dataclass(frozen=True)
class GenerationClaim:
    """Everything the Delegator turn needs, plus the id its progress is on.

    Unlike a context or a review, decomposition has no row until it succeeds —
    the delegation revision is created from the turn's output. So the claim
    carries a generated `job_id` instead, and `delegation.progress` events on
    that id are what a reader follows while the turn runs.
    """

    job_id: str
    session_id: str
    sandbox_id: str
    volume_name: str
    prompt: str
    command_kinds: frozenset[str]
    turn_settings: PlanningSettings
    provider: AgentProvider
    project_name: str | None


def claim_generation(
    planning_settings: PlanningSettings,
    settings: DelegatorSettings,
    store: ControllerStore,
    session_id: str,
    request: GenerateDelegationRequest,
    *,
    project_name: str | None = None,
) -> GenerationClaim:
    """Check a decomposition can run and reserve a progress id for it."""
    session = _delegatable_session(store, session_id, project_name=project_name)
    if _active_delegation(store, str(session["sandbox_id"])) is not None:
        raise DelegationOperationError(
            409,
            "This sandbox already has an active delegation",
        )

    plan = json_object(session.get("plan_spec_json"))
    if plan is None:
        raise DelegationOperationError(
            409, "Planning session has no plan specification"
        )
    context = ready_context(store, session_id)
    if context is None:
        raise DelegationOperationError(
            409,
            "Generate implementation context before delegating",
        )

    kinds = frozenset(context.confirmed_commands)
    if not kinds:
        raise DelegationOperationError(
            409,
            "Implementation context has no confirmed verification commands",
        )
    sandbox = store.sandbox(str(session["sandbox_id"]))
    if sandbox is None:
        raise DelegationOperationError(404, "Planning sandbox was not found")

    job_id = uuid4().hex
    _progress(
        store,
        job_id,
        str(session["sandbox_id"]),
        step="claimed",
        message="Decomposition reserved",
    )
    return GenerationClaim(
        job_id=job_id,
        session_id=session_id,
        sandbox_id=str(session["sandbox_id"]),
        volume_name=str(sandbox["volume_name"]),
        prompt=prompts.delegator_prompt(
            str(session["title"]),
            plan,
            context.manifest.model_dump() if context.manifest else {},
            sorted(kinds),
        ),
        command_kinds=kinds,
        turn_settings=_effective_turn_settings(
            planning_settings,
            settings,
            session,
            request,
        ),
        provider=request.provider,
        project_name=project_name,
    )


def fail_generation_claim(
    store: ControllerStore,
    claim: GenerationClaim,
    detail: str,
) -> None:
    """Report a decomposition whose turn never started. See jobs.submit_docker_job.

    There is no row to settle here — decomposition only creates one on success —
    so the failure lives on the progress stream the reader is already watching.
    """
    _progress(
        store,
        claim.job_id,
        claim.sandbox_id,
        step="failed",
        message=detail,
        level="error",
    )


def _progress(
    store: ControllerStore,
    job_id: str,
    sandbox_id: str,
    *,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    store.progress_event(
        sandbox_id=sandbox_id,
        run_id=job_id,
        kind="delegation.progress",
        step=step,
        message=message,
        level=level,
    )


def execute_generation(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: GenerationClaim,
) -> GenerateDelegationOutcome:
    """Run the Delegator behind a claim. Safe to run off-request."""
    _progress(
        store,
        claim.job_id,
        claim.sandbox_id,
        step="turn",
        message=f"Running the {claim.provider.value} decomposition turn",
    )
    try:
        turn = run_validated_turn(
            lambda prompt: run_planning_turn(
                docker_client,
                claim.turn_settings,
                TurnRequest(
                    role=PlanningRole.DELEGATOR,
                    provider=claim.provider,
                    prompt=prompt,
                    project_volume=claim.volume_name,
                    session_id=claim.session_id,
                ),
            ),
            prompt=claim.prompt,
            validate=lambda payload: _payload_errors(payload, claim.command_kinds),
        )
    except Exception as error:
        _progress(
            store,
            claim.job_id,
            claim.sandbox_id,
            step="failed",
            message=str(error) or "Decomposition turn did not complete",
            level="error",
        )
        raise

    if turn.result is None or turn.errors:
        _progress(
            store,
            claim.job_id,
            claim.sandbox_id,
            step="settled",
            message="; ".join(turn.errors) or "Decomposition produced no usable output",
            level="error",
        )
        return GenerateDelegationOutcome(
            delegation=None,
            accepted=False,
            attempts=turn.attempts,
            validation_errors=turn.errors,
            turn_status="invalid_output" if turn.result is None else "succeeded",
            turn_error="; ".join(turn.errors) or None,
            model=turn.result.model if turn.result is not None else None,
        )

    try:
        created = create_revision(
            store,
            claim.session_id,
            list(turn.result.payload.get("items") or []),
            project_name=claim.project_name,
        )
    except DelegationOperationError as error:
        _progress(
            store,
            claim.job_id,
            claim.sandbox_id,
            step="failed",
            message=error.detail,
            level="error",
        )
        raise

    _progress(
        store,
        claim.job_id,
        claim.sandbox_id,
        step="settled",
        message=f"Decomposition produced {len(created.items)} work items",
    )
    return GenerateDelegationOutcome(
        delegation=created,
        accepted=True,
        attempts=turn.attempts,
        turn_status="succeeded",
        model=turn.result.model,
    )


def generate_revision(
    docker_client: DockerClient,
    planning_settings: PlanningSettings,
    settings: DelegatorSettings,
    store: ControllerStore,
    session_id: str,
    request: GenerateDelegationRequest,
    *,
    project_name: str | None = None,
) -> GenerateDelegationOutcome:
    """Run the Delegator and retain a valid decomposition, start to finish."""
    claim = claim_generation(
        planning_settings,
        settings,
        store,
        session_id,
        request,
        project_name=project_name,
    )
    return execute_generation(docker_client, store, claim)


def _payload_errors(
    payload: Mapping[str, Any],
    command_kinds: frozenset[str],
) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list):
        return ["'items' is required and must be a list of work items"]
    return graph.validate_work_items(
        items,
        available_command_kinds=command_kinds,
    )


def _active_delegation(
    store: ControllerStore,
    sandbox_id: str,
) -> dict[str, Any] | None:
    active = {status.value for status in ACTIVE_DELEGATION_STATUSES}
    for row in store.delegations_for_sandbox(sandbox_id):
        if str(row["status"]) in active:
            return row
    return None


def view(
    store: ControllerStore,
    delegation_id: str,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> DelegationView:
    from app.delegation.integration_review import latest_review

    row = _delegation_row(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    items = [_work_item(item) for item in store.work_items(delegation_id)]
    runs = [_run(store, run) for run in store.work_item_runs(delegation_id)]
    by_item: dict[str, list[WorkItemRun]] = {}
    for run in runs:
        by_item.setdefault(run.work_item_id, []).append(run)

    edges = {item.key: list(item.dependencies) for item in items}
    statuses = {
        item.key: [run.status for run in by_item.get(item.id, [])] for item in items
    }
    states = graph.item_states(edges, statuses)
    computed = graph.waves(edges)
    wave_of = {key: index for index, wave in enumerate(computed) for key in wave}
    overrides = store.work_item_routing(delegation_id)
    routing_settings = get_routing_settings()
    item_views = [
        WorkItemView(
            item=item,
            state=states[item.key],
            wave=wave_of.get(item.key, 0),
            blocked_by=graph.blocked_by(item.key, edges, states),
            can_run_in_parallel_with=graph.parallel_candidates(item.key, edges),
            runs=sorted(by_item.get(item.id, []), key=lambda run: run.attempt),
            routing=_routing(
                item,
                overrides.get(item.id, {}),
                routing_settings,
            ),
        )
        for item in items
    ]
    review = latest_review(store, delegation_id)
    changes = [
        _change_request(change)
        for change in store.delegation_change_requests(delegation_id)
    ]
    review_superseded = _review_superseded(
        review,
        _latest_incorporated_change(changes),
    )
    return DelegationView(
        delegation=_delegation(row),
        items=item_views,
        waves=computed,
        ready=sorted(item.key for item in items if states[item.key].value == "ready"),
        review=review,
        changes=changes,
        review_superseded=review_superseded,
        feature_approved=_feature_approved(review, review_superseded),
    )


def list_delegations(
    store: ControllerStore,
    session_id: str,
    *,
    project_name: str | None = None,
) -> list[Delegation]:
    _planning_session(store, session_id, project_name=project_name)
    return [_delegation(row) for row in store.delegations_for_session(session_id)]


def transition(
    store: ControllerStore,
    delegation_id: str,
    to_status: DelegationStatus,
    *,
    error: str | None = None,
    session_id: str | None = None,
    project_name: str | None = None,
) -> DelegationView:
    row = _delegation_row(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    current = DelegationStatus(str(row["status"]))
    if current in TERMINAL_DELEGATION_STATUSES:
        raise DelegationOperationError(
            409,
            f"Delegation is already settled as '{current.value}'",
        )
    updated = store.transition_delegation(
        delegation_id,
        to_status=to_status.value,
        from_statuses=[
            status.value for status in delegation_source_statuses(to_status)
        ],
        terminal=to_status in TERMINAL_DELEGATION_STATUSES,
        error=error,
    )
    if updated is None:
        raise DelegationOperationError(
            409,
            f"Cannot move a delegation from '{current.value}' to '{to_status.value}'",
        )
    return view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )


def set_routing(
    store: ControllerStore,
    delegation_id: str,
    key: str,
    request: Any,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> DelegationView:
    _delegation_row(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    item = _require_item(store, delegation_id, key)
    if request.provider is None and request.model is None:
        store.clear_work_item_routing(item.id)
    else:
        store.set_work_item_routing(
            item.id,
            provider=request.provider.value if request.provider else None,
            model=request.model,
            actor=request.actor,
        )
    return view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )


def clear_routing(
    store: ControllerStore,
    delegation_id: str,
    key: str,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> DelegationView:
    _delegation_row(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    item = _require_item(store, delegation_id, key)
    store.clear_work_item_routing(item.id)
    return view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )


def _require_item(
    store: ControllerStore,
    delegation_id: str,
    key: str,
) -> WorkItem:
    for row in store.work_items(delegation_id):
        if str(row["key"]) == key:
            return _work_item(row)
    raise DelegationOperationError(404, f"Work item '{key}' was not found")


def _routing(
    item: WorkItem,
    override: Mapping[str, Any],
    settings: Any,
) -> ItemRouting:
    override_provider = _provider(override.get("provider"))
    decision = route(
        item.complexity,
        settings,
        item_provider=override_provider,
        item_model=override.get("model"),
    )
    return ItemRouting(
        recommended_model=decision.recommended_model,
        model=decision.model,
        source=decision.source.value,
        provider=decision.provider,
        override_provider=override_provider,
        override_model=override.get("model"),
        warning=decision.warning,
        models_by_provider={
            provider: list(models) for provider, models in settings.catalogue().items()
        },
        recommended_by_provider={
            provider.value: settings.for_provider(provider).for_complexity(
                item.complexity
            )
            for provider in AgentProvider
        },
    )


def _provider(value: Any) -> AgentProvider | None:
    try:
        return AgentProvider(str(value)) if value else None
    except ValueError:
        return None


def _delegation_row(
    store: ControllerStore,
    delegation_id: str,
    *,
    session_id: str | None,
    project_name: str | None,
) -> dict[str, Any]:
    row = store.delegation(delegation_id)
    if row is None or (session_id is not None and str(row["session_id"]) != session_id):
        raise DelegationOperationError(404, "Delegation was not found")
    if project_name is not None:
        _planning_session(store, str(row["session_id"]), project_name=project_name)
    return row


def _delegatable_session(
    store: ControllerStore,
    session_id: str,
    *,
    project_name: str | None,
) -> dict[str, Any]:
    session = _planning_session(store, session_id, project_name=project_name)
    if str(session["status"]) not in _DELEGATABLE_SESSION_STATUSES:
        raise DelegationOperationError(
            409,
            "A delegation needs a completed plan, "
            f"but this session is '{session['status']}'",
        )
    return session


def _planning_session(
    store: ControllerStore,
    session_id: str,
    *,
    project_name: str | None,
) -> dict[str, Any]:
    session = store.planning_session(session_id)
    if session is None:
        raise DelegationOperationError(404, "Planning session was not found")
    if (
        project_name is not None
        and str(session["project_name"]).casefold() != project_name.casefold()
    ):
        raise DelegationOperationError(404, "Planning session was not found")
    return session


def _effective_turn_settings(
    planning_settings: PlanningSettings,
    delegator_settings: DelegatorSettings,
    session: Mapping[str, Any],
    request: GenerateDelegationRequest,
) -> PlanningSettings:
    changes: dict[str, Any] = {
        "credential_profile": str(session["credential_profile"]),
    }
    if request.provider is AgentProvider.CLAUDE:
        changes["claude_model"] = request.model or delegator_settings.model
    else:
        changes["codex_model"] = request.model or planning_settings.codex_model
    return replace(planning_settings, **changes)


def _work_item_row(
    delegation_id: str,
    position: int,
    item: Mapping[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4().hex,
        "delegation_id": delegation_id,
        "key": str(item["key"]),
        "position": position,
        "title": str(item["title"]),
        "objective": str(item["objective"]),
        "scope": str(item["scope"]),
        "out_of_scope": str(item.get("out_of_scope") or ""),
        "complexity": str(item["complexity"]),
        "verification_json": json.dumps(list(item.get("verification") or [])),
    }
    for field, column in _LIST_COLUMNS.items():
        row[column] = json.dumps(list(item.get(field) or []))
    return row


def _work_item(row: Mapping[str, Any]) -> WorkItem:
    values: dict[str, Any] = {
        field: _list(row.get(column)) for field, column in _LIST_COLUMNS.items()
    }
    return WorkItem(
        id=str(row["id"]),
        delegation_id=str(row["delegation_id"]),
        key=str(row["key"]),
        position=int(row["position"]),
        title=str(row["title"]),
        objective=str(row["objective"]),
        scope=str(row["scope"]),
        out_of_scope=str(row["out_of_scope"] or ""),
        complexity=str(row["complexity"]),
        verification=[
            VerificationIntent.model_validate(intent)
            for intent in _list(row.get("verification_json"))
            if isinstance(intent, dict)
        ],
        created_at=str(row["created_at"]),
        **values,
    )


def _run(store: ControllerStore, row: Mapping[str, Any]) -> WorkItemRun:
    task_id = row.get("task_id")
    task = store.task(str(task_id)) if task_id else None
    return WorkItemRun(
        id=str(row["id"]),
        work_item_id=str(row["work_item_id"]),
        delegation_id=str(row["delegation_id"]),
        attempt=int(row["attempt"]),
        status=RunStatus(str(row["status"])),
        provider=row.get("provider"),
        model=row.get("model"),
        routing_source=row.get("routing_source"),
        task_id=task_id,
        task_status=str(task["status"]) if task else None,
        result=json_object(row.get("result_json")),
        failure_kind=row.get("failure_kind"),
        error=row.get("error"),
        verification=json_object(row.get("verification_json")),
        usage=RunUsage(
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_creation_tokens=row.get("cache_creation_tokens"),
            cost_usd=row.get("cost_usd"),
        ),
        duration_ms=row.get("duration_ms"),
        exit_code=row.get("exit_code"),
        repair_count=int(row.get("repair_count") or 0),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=row.get("settled_at"),
    )


def _delegation(row: Mapping[str, Any]) -> Delegation:
    return Delegation(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        sandbox_id=str(row["sandbox_id"]),
        context_id=row.get("context_id"),
        revision=int(row["revision"]),
        status=DelegationStatus(str(row["status"])),
        error=row.get("error"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=row.get("settled_at"),
    )


def _change_request(row: Mapping[str, Any]) -> FeatureChangeRequest:
    return FeatureChangeRequest(
        id=str(row["id"]),
        delegation_id=str(row["delegation_id"]),
        revision=int(row["revision"]),
        status=ChangeRequestStatus(str(row["status"])),
        instructions=str(row["instructions"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        task_id=row.get("task_id"),
        verification=json_object(row.get("verification_json")),
        error=row.get("error"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=row.get("settled_at"),
    )


def _latest_incorporated_change(
    changes: Sequence[FeatureChangeRequest],
) -> FeatureChangeRequest | None:
    for change in reversed(changes):
        if change.status in {
            ChangeRequestStatus.AWAITING_REVIEW,
            ChangeRequestStatus.COMPLETED,
        }:
            return change
    return None


def _review_superseded(
    review: IntegrationReview | None,
    change: FeatureChangeRequest | None,
) -> bool:
    if review is None or review.settled_at is None or change is None:
        return False
    try:
        return datetime.fromisoformat(change.created_at) > datetime.fromisoformat(
            review.settled_at
        )
    except ValueError:
        return False


def _feature_approved(
    review: IntegrationReview | None,
    superseded: bool,
) -> bool:
    return bool(
        review
        and review.status is IntegrationReviewStatus.COMPLETED
        and review.approved is True
        and not superseded
    )


def _list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


__all__ = [
    "ACTIVE_DELEGATION_STATUSES",
    "DelegationOperationError",
    "clear_routing",
    "confirmed_command_kinds",
    "create_revision",
    "generate_revision",
    "list_delegations",
    "set_routing",
    "transition",
    "view",
]
