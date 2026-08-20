"""Feature-level review after every delegated work item is merged."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

from app.containers.config import get_git_settings
from app.controller.store import ControllerStore, ReviewGenerating, RevisionTaken
from app.controller.store.delegation_status import DelegationStatus
from app.delegation import service
from app.delegation.config import IntegrationReviewSettings
from app.delegation.delivery import capture_feature_target
from app.delegation.models import (
    GenerateIntegrationReviewOutcome,
    GenerateIntegrationReviewRequest,
    IntegrationFinding,
    IntegrationReview,
    IntegrationReviewStatus,
)
from app.planning.config import PlanningSettings
from app.planning.models import PlanningRole
from app.planning.runner import TurnRequest, run_planning_turn, run_validated_turn
from app.platform.coercions import json_object
from app.sandboxes.feature_target import (
    FeatureTargetError,
    ensure_target_unchanged,
)


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
    evidence_findings: list[dict[str, Any]]
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
    plan = json_object(session.get("plan_spec_json"))
    if plan is None:
        raise service.DelegationOperationError(
            409, "Planning session has no plan specification"
        )
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
        store.claim_delegation_review(
            {
                "id": review_id,
                "delegation_id": delegation_id,
                "status": IntegrationReviewStatus.GENERATING.value,
                "provider": request.provider.value,
                "model": model,
            }
        )
    except RevisionTaken as error:
        raise service.DelegationOperationError(
            409,
            "This integration review revision was claimed concurrently",
        ) from error
    except ReviewGenerating as error:
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
        evidence_findings=_change_evidence_findings(delegation_view),
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
    store.progress_event(
        sandbox_id=sandbox_id,
        run_id=review_id,
        kind="review.progress",
        step=step,
        message=message,
        level=level,
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
    git_image = get_git_settings().git_image
    try:
        target = capture_feature_target(
            docker_client,
            git_image,
            store,
            service.view(store, delegation_id),
        )
        pinned = store.pin_delegation_review_target(
            review_id,
            base_branch=target.base_branch,
            base_commit=target.base_commit,
            head_commit=target.head_commit,
        )
        if pinned is None:
            raise service.DelegationOperationError(
                409,
                "Feature review target was not reserved",
            )
    except Exception as error:
        detail = (
            str(getattr(error, "detail", error))
            or "Feature review target is unavailable"
        )
        store.settle_delegation_review(
            review_id,
            to_status=IntegrationReviewStatus.FAILED.value,
            error=detail[:1500],
        )
        _progress(
            store,
            review_id,
            claim.sandbox_id,
            step="failed",
            message=detail,
            level="error",
        )
        raise
    _progress(
        store,
        review_id,
        claim.sandbox_id,
        step="turn",
        message=f"Running the {claim.request.provider.value} review turn",
    )
    try:
        prompt = (
            f"{claim.prompt}\n\nReview target: branch {target.base_branch}, "
            f"commits {target.base_commit}..{target.head_commit}."
        )
        turn = run_validated_turn(
            lambda current_prompt: run_planning_turn(
                docker_client,
                claim.turn_settings,
                TurnRequest(
                    role=PlanningRole.INTEGRATION_REVIEWER,
                    provider=claim.request.provider,
                    prompt=current_prompt,
                    project_volume=claim.volume_name,
                    session_id=delegation_id,
                ),
            ),
            prompt=prompt,
            validate=lambda payload: _validate(payload, claim.item_keys),
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
            error="; ".join(turn.errors)[:1500] or "Review produced no usable output",
        )
        _progress(
            store,
            review_id,
            claim.sandbox_id,
            step="settled",
            message="; ".join(turn.errors) or "Review produced no usable output",
            level="error",
        )
        return GenerateIntegrationReviewOutcome(
            review=review_from_row(store.delegation_reviews(delegation_id)[0]),
            accepted=False,
            attempts=turn.attempts,
            validation_errors=turn.errors,
            turn_status="invalid_output" if turn.result is None else "succeeded",
            turn_error="; ".join(turn.errors) or None,
        )

    try:
        ensure_target_unchanged(
            docker_client,
            git_image,
            store,
            claim.sandbox_id,
            target,
        )
    except FeatureTargetError as error:
        store.settle_delegation_review(
            review_id,
            to_status=IntegrationReviewStatus.FAILED.value,
            model=turn.result.model,
            error=error.detail[:1500],
        )
        _progress(
            store,
            review_id,
            claim.sandbox_id,
            step="failed",
            message=error.detail,
            level="error",
        )
        return GenerateIntegrationReviewOutcome(
            review=review_from_row(store.delegation_reviews(delegation_id)[0]),
            accepted=False,
            attempts=turn.attempts,
            validation_errors=[error.detail],
            turn_status="repository_changed",
        )

    result_payload = dict(turn.result.payload)
    if claim.evidence_findings:
        result_payload["approved"] = False
        result_payload["summary"] = (
            "Whole-feature approval is on hold because requested changes lack "
            "complete acceptance evidence."
        )
        result_payload["findings"] = [
            *result_payload.get("findings", []),
            *claim.evidence_findings,
        ]
    # A reviewer that approves while naming a high or medium finding contradicts
    # itself, and the approval is the half the controller acts on. Take the
    # findings as the verdict and keep the reviewer's own words in the summary.
    elif result_payload.get("approved") is True and _has_serious_finding(
        result_payload
    ):
        result_payload["approved"] = False
        result_payload["summary"] = (
            "The reviewer verdict was overridden because it approved the feature "
            "while raising a high or medium finding. Reviewer summary: "
            + str(result_payload.get("summary", "")).strip()
        )

    store.settle_delegation_review(
        review_id,
        to_status=IntegrationReviewStatus.COMPLETED.value,
        result_json=json.dumps(result_payload),
        model=turn.result.model,
    )
    if result_payload["approved"] is True:
        store.complete_awaiting_delegation_changes(
            delegation_id,
            review_id=review_id,
        )
    _progress(
        store,
        review_id,
        claim.sandbox_id,
        step="settled",
        message="Integration review is complete",
    )
    return GenerateIntegrationReviewOutcome(
        review=review_from_row(store.delegation_reviews(delegation_id)[0]),
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
        if (
            not isinstance(finding.get("text"), str)
            or not finding.get("text", "").strip()
        ):
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
    changes = [
        {
            "revision": change.revision,
            "status": change.status.value,
            "instructions": change.instructions,
            "provider": change.provider.value,
            "model": change.model,
            "verification": _change_verification_summary(change.verification),
        }
        for change in delegation_view.changes
    ]
    return f"""Review the completed feature as a whole.

Compare the final repository state with the reviewed plan, item acceptance criteria,
controller-run verification, retained implementation results, and every requested
feature change.

Reviewed plan:
{json.dumps(plan, indent=2)}

Completed work:
{json.dumps(results, indent=2)}

Requested feature changes:
{json.dumps(changes, indent=2)}

Review the delivered code for:

1. Requirements coverage. Does the feature satisfy every requirement in the plan, every
   item's acceptance criteria, and every requested change? Is any user-visible behavior
   missing or left unspecified?
2. Repository correctness. Do the paths, symbols, and APIs the work names exist and behave
   as the work assumes? Read the current file before you raise or dismiss a finding.
3. Architectural fit. Does the code follow the abstractions and conventions this repository
   already uses? Does it introduce a concept the feature does not need?
4. Completeness. Error paths, edge cases, state transitions, concurrency and races where
   relevant, backwards compatibility, and migrations or data integrity where relevant.
5. Integration. Do the separately implemented items fit together? Look for a leftover stub,
   a new symbol nothing calls, a caller that was never updated, and two items that solve
   the same problem twice.
6. Verification. Does the evidence make the desired behavior objectively testable, and does
   it prove it? Are the important regression cases covered?
7. Scope discipline. Is anything unnecessary included? Is anything required left out?

Treat acceptance evidence as part of the review gate. Do not approve an interactive
or user-visible change when its only evidence is a build, typecheck, lint, or static
inspection. Check related markup, styling, state transitions, and timing. When the
evidence cannot prove the requested behavior, return approved=false with a specific
finding. Use an empty work_item_keys list for a finding about a feature change.

Cite a repository fact only if you read it, and give the path and line range. Do not claim
to have run, compiled, built, or tested anything. Judge execution only from the verification
records above.

Severity: high blocks the feature, medium is a real defect a maintainer must fix before
release, low is a nit. Approve only when no high or medium finding stands. A low finding may
remain open when it does not affect correctness, architecture, requirements, or the
delivered behavior.

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


def _has_serious_finding(payload: Mapping[str, Any]) -> bool:
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return False
    return any(
        isinstance(finding, Mapping) and finding.get("severity") in {"high", "medium"}
        for finding in findings
    )


def _change_evidence_findings(delegation_view: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    # Only the newest pending change describes the tree under review. An
    # earlier revision's shortcomings were answered by the one that replaced
    # it, and a change request has no settled state to leave, so judging every
    # pending revision let one weak turn block the delegation permanently.
    pending = [
        change
        for change in delegation_view.changes
        if change.status.value == "awaiting_review"
    ]
    latest = max(pending, key=lambda change: change.revision, default=None)
    for change in [latest] if latest is not None else []:
        verification = change.verification
        evidence = (
            verification.get("acceptance_evidence")
            if isinstance(verification, Mapping)
            else None
        )
        if isinstance(evidence, Mapping) and evidence.get("complete") is True:
            continue
        reasons = evidence.get("errors", []) if isinstance(evidence, Mapping) else []
        detail = "; ".join(str(reason) for reason in reasons if str(reason).strip())
        suffix = f": {detail}" if detail else ""
        findings.append(
            {
                "severity": "high",
                "text": (
                    f"Feature change revision {change.revision} lacks complete "
                    f"acceptance evidence{suffix}"
                ),
                "work_item_keys": [],
            }
        )
    return findings


def _change_verification_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    commands = []
    for command in value.get("commands", []):
        if not isinstance(command, Mapping):
            continue
        commands.append(
            {
                key: command.get(key)
                for key in ("command_kind", "command", "passed", "detail")
                if command.get(key) is not None
            }
        )
    return {
        "passed": value.get("passed"),
        "commands": commands,
        "acceptance_evidence": value.get("acceptance_evidence"),
        "agent_report": value.get("agent_report"),
        "turn": value.get("turn"),
    }


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


def review_from_row(row: Mapping[str, Any]) -> IntegrationReview:
    result = json_object(row.get("result_json")) or {}
    return IntegrationReview(
        id=str(row["id"]),
        delegation_id=str(row["delegation_id"]),
        revision=int(row["revision"]),
        status=str(row["status"]),
        provider=row.get("provider"),
        model=row.get("model"),
        base_branch=row.get("base_branch"),
        base_commit=row.get("base_commit"),
        head_commit=row.get("head_commit"),
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
        source_merged_at=row.get("source_merged_at"),
    )


def latest_review(
    store: ControllerStore, delegation_id: str
) -> IntegrationReview | None:
    rows = store.delegation_reviews(delegation_id)
    return review_from_row(rows[0]) if rows else None
