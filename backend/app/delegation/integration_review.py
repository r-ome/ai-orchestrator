"""Feature-level review after every delegated work item is merged."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

from app.controller.store import ControllerStore
from app.delegation import service
from app.delegation.config import IntegrationReviewSettings
from app.delegation.models import (
    DelegationStatus,
    GenerateIntegrationReviewOutcome,
    GenerateIntegrationReviewRequest,
    IntegrationFinding,
    IntegrationReview,
    IntegrationReviewStatus,
)
from app.planning.config import PlanningSettings
from app.planning.models import PlanningRole
from app.planning.runner import PlanningTurnError, TurnRequest, TurnResult, run_planning_turn


@dataclass(frozen=True)
class _ReviewTurn:
    result: TurnResult | None
    attempts: int
    errors: list[str]
    error: str | None = None


@dataclass(frozen=True)
class ReviewClaim:
    """A generating review row plus what its turn needs. See ContextClaim."""

    review_id: str
    delegation_id: str
    sandbox_id: str
    volume_name: str
    model: str
    prompt: str
    item_keys: set[str]
    turn_settings: PlanningSettings
    request: GenerateIntegrationReviewRequest


def claim_integration_review(
    planning_settings: PlanningSettings,
    settings: IntegrationReviewSettings,
    store: ControllerStore,
    delegation_id: str,
    request: GenerateIntegrationReviewRequest,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> ReviewClaim:
    delegation_view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    if delegation_view.delegation.status is not DelegationStatus.COMPLETED:
        raise service.DelegationOperationError(
            409,
            "Integration review requires a completed delegation",
        )
    session = store.planning_session(delegation_view.delegation.session_id)
    if session is None:
        raise service.DelegationOperationError(404, "Planning session was not found")
    plan = _object(session.get("plan_spec_json"))
    if plan is None:
        raise service.DelegationOperationError(409, "Planning session has no plan specification")
    sandbox = store.sandbox(delegation_view.delegation.sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(404, "Delegation sandbox was not found")

    review_id = uuid4().hex
    model = request.model or (
        settings.model
        if request.provider.value == "claude"
        else planning_settings.codex_model
    )
    try:
        store.create_delegation_review(
            {
                "id": review_id,
                "delegation_id": delegation_id,
                "revision": store.next_delegation_review_revision(delegation_id),
                "status": IntegrationReviewStatus.GENERATING.value,
                "provider": request.provider.value,
                "model": model,
            }
        )
    except sqlite3.IntegrityError as error:
        raise service.DelegationOperationError(
            409,
            "An integration review is already running",
        ) from error

    _progress(
        store,
        review_id,
        delegation_view.delegation.sandbox_id,
        step="claimed",
        message="Integration review reserved",
    )
    return ReviewClaim(
        review_id=review_id,
        delegation_id=delegation_id,
        sandbox_id=delegation_view.delegation.sandbox_id,
        volume_name=str(sandbox["volume_name"]),
        model=model,
        prompt=_prompt(plan, delegation_view),
        item_keys={entry.item.key for entry in delegation_view.items},
        turn_settings=_settings(planning_settings, session, request, model),
        request=request,
    )


def fail_review_claim(
    store: ControllerStore,
    claim: ReviewClaim,
    detail: str,
) -> None:
    """Settle a review whose turn never started. See jobs.submit_docker_job."""
    store.settle_delegation_review(
        claim.review_id,
        to_status=IntegrationReviewStatus.FAILED.value,
        error=detail[:1500],
    )
    _progress(
        store,
        claim.review_id,
        claim.sandbox_id,
        step="failed",
        message=detail,
        level="error",
    )


def _progress(
    store: ControllerStore,
    review_id: str,
    sandbox_id: str,
    *,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    store.event(
        sandbox_id=sandbox_id,
        run_id=review_id,
        kind="review.progress",
        payload={"step": step, "message": message[:900], "level": level},
    )


def execute_integration_review(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: ReviewClaim,
) -> GenerateIntegrationReviewOutcome:
    """Run the turn behind a claim and settle its row. Safe to run off-request."""
    delegation_id = claim.delegation_id
    review_id = claim.review_id
    model = claim.model
    _progress(
        store,
        review_id,
        claim.sandbox_id,
        step="turn",
        message=f"Running the {claim.request.provider.value} review turn",
    )
    try:
        turn = _run_turn(
            docker_client,
            claim.turn_settings,
            claim.request,
            claim.volume_name,
            delegation_id,
            claim.prompt,
            claim.item_keys,
        )
    except Exception as error:
        store.settle_delegation_review(
            review_id,
            to_status=IntegrationReviewStatus.FAILED.value,
            error="Integration review turn did not complete",
        )
        _progress(
            store,
            review_id,
            claim.sandbox_id,
            step="failed",
            message=str(error) or "Integration review turn did not complete",
            level="error",
        )
        raise

    if turn.result is None or turn.errors:
        store.settle_delegation_review(
            review_id,
            to_status=IntegrationReviewStatus.FAILED.value,
            model=turn.result.model if turn.result else model,
            error="; ".join(turn.errors)[:1500] or turn.error,
        )
        _progress(
            store,
            review_id,
            claim.sandbox_id,
            step="settled",
            message="; ".join(turn.errors) or turn.error or "Review produced no usable output",
            level="error",
        )
        return GenerateIntegrationReviewOutcome(
            review=_review(store.delegation_reviews(delegation_id)[0]),
            accepted=False,
            attempts=turn.attempts,
            validation_errors=turn.errors,
            turn_status="invalid_output" if turn.result is None else "succeeded",
            turn_error=turn.error,
        )

    store.settle_delegation_review(
        review_id,
        to_status=IntegrationReviewStatus.COMPLETED.value,
        result_json=json.dumps(turn.result.payload),
        model=turn.result.model,
    )
    _progress(
        store,
        review_id,
        claim.sandbox_id,
        step="settled",
        message="Integration review is complete",
    )
    return GenerateIntegrationReviewOutcome(
        review=_review(store.delegation_reviews(delegation_id)[0]),
        accepted=True,
        attempts=turn.attempts,
        validation_errors=[],
        turn_status="succeeded",
    )


def generate_integration_review(
    docker_client: DockerClient,
    planning_settings: PlanningSettings,
    settings: IntegrationReviewSettings,
    store: ControllerStore,
    delegation_id: str,
    request: GenerateIntegrationReviewRequest,
    *,
    session_id: str | None = None,
    project_name: str | None = None,
) -> GenerateIntegrationReviewOutcome:
    """Claim and run a feature-level review, start to finish."""
    claim = claim_integration_review(
        planning_settings,
        settings,
        store,
        delegation_id,
        request,
        session_id=session_id,
        project_name=project_name,
    )
    return execute_integration_review(docker_client, store, claim)


def _run_turn(
    docker_client: DockerClient,
    settings: PlanningSettings,
    request: GenerateIntegrationReviewRequest,
    volume_name: str,
    delegation_id: str,
    prompt: str,
    item_keys: set[str],
) -> _ReviewTurn:
    current = prompt
    errors: list[str] = []
    for attempt in range(1, 3):
        try:
            result = run_planning_turn(
                docker_client,
                settings,
                TurnRequest(
                    role=PlanningRole.INTEGRATION_REVIEWER,
                    provider=request.provider,
                    prompt=current,
                    project_volume=volume_name,
                    session_id=delegation_id,
                ),
            )
            current_errors = _validate(result.payload, item_keys)
            raw = result.raw_output
        except PlanningTurnError as error:
            if error.status_code != 422:
                raise
            result = None
            current_errors = [error.detail]
            raw = error.raw_output
        errors.extend(error for error in current_errors if error not in errors)
        if not current_errors:
            return _ReviewTurn(result, attempt, [])
        if attempt == 2:
            return _ReviewTurn(
                result,
                attempt,
                errors,
                current_errors[0] if result is None else None,
            )
        current = _repair_prompt(prompt, current_errors, raw)
    raise AssertionError("integration review loop must return")


def _validate(payload: Mapping[str, Any], item_keys: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload.get("approved"), bool):
        errors.append("'approved' must be a boolean")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("'summary' must be a non-empty string")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return [*errors, "'findings' must be a list"]
    for index, finding in enumerate(findings):
        label = f"findings[{index}]"
        if not isinstance(finding, Mapping):
            errors.append(f"{label} must be an object")
            continue
        if finding.get("severity") not in {"low", "medium", "high"}:
            errors.append(f"{label}.severity must be low, medium, or high")
        if not isinstance(finding.get("text"), str) or not finding.get("text", "").strip():
            errors.append(f"{label}.text must be a non-empty string")
        keys = finding.get("work_item_keys")
        if not isinstance(keys, list) or any(
            not isinstance(key, str) or key not in item_keys for key in keys
        ):
            errors.append(f"{label}.work_item_keys must contain known item keys")
    return errors


def _prompt(plan: Mapping[str, Any], delegation_view: Any) -> str:
    results = [
        {
            "key": entry.item.key,
            "title": entry.item.title,
            "acceptance_criteria": entry.item.acceptance_criteria,
            "result": entry.runs[-1].result if entry.runs else None,
            "verification": entry.runs[-1].verification if entry.runs else None,
        }
        for entry in delegation_view.items
    ]
    return f"""Review the completed feature as a whole.

Compare the final repository state with the reviewed plan, item acceptance criteria,
controller-run verification, and retained implementation results.

Reviewed plan:
{json.dumps(plan, indent=2)}

Completed work:
{json.dumps(results, indent=2)}

Inspect current files when needed. Do not modify the repository.
Return exactly one JSON object:
{{
  "approved": true,
  "summary": "feature-level conclusion",
  "findings": [
    {{"severity": "low", "text": "specific finding", "work_item_keys": ["item-key"]}}
  ]
}}
Use an empty findings list when approved.
"""


def _repair_prompt(original: str, errors: list[str], raw: str) -> str:
    return (
        f"{original}\n\nYour previous response was invalid:\n"
        + "\n".join(f"- {error}" for error in errors)
        + f"\n\nPrevious output:\n{raw[-8000:]}\n\nReturn only corrected JSON."
    )


def _settings(
    settings: PlanningSettings,
    session: Mapping[str, Any],
    request: GenerateIntegrationReviewRequest,
    model: str,
) -> PlanningSettings:
    changes: dict[str, Any] = {
        "credential_profile": str(session["credential_profile"]),
    }
    if request.provider.value == "claude":
        changes["claude_model"] = model
    else:
        changes["codex_model"] = model
    return replace(settings, **changes)


def _review(row: Mapping[str, Any]) -> IntegrationReview:
    result = _object(row.get("result_json")) or {}
    return IntegrationReview(
        id=str(row["id"]),
        delegation_id=str(row["delegation_id"]),
        revision=int(row["revision"]),
        status=str(row["status"]),
        provider=row.get("provider"),
        model=row.get("model"),
        approved=result.get("approved"),
        summary=str(result.get("summary") or ""),
        findings=[
            IntegrationFinding.model_validate(finding)
            for finding in result.get("findings", [])
            if isinstance(finding, dict)
        ],
        error=row.get("error"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=row.get("settled_at"),
    )


def latest_review(store: ControllerStore, delegation_id: str) -> IntegrationReview | None:
    rows = store.delegation_reviews(delegation_id)
    return _review(rows[0]) if rows else None


def _object(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
