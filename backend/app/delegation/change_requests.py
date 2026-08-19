"""Agent changes requested against a completed feature implementation."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from docker.client import DockerClient

from app.platform.coercions import json_object
from app.controller.store import (
    ChangeRequestRunning,
    ControllerStore,
    RevisionTaken,
)
from app.delegation import service
from app.delegation.config import get_verification_settings
from app.delegation.models import (
    ChangeRequestStatus,
    DelegationStatus,
    FeatureChangeRequest,
    RequestFeatureChange,
)
from app.delegation.packet import ResolvedVerification
from app.delegation.verification import run_verification
from app.implementation_context.service import ContextOperationError, get_context
from app.tasks.config import CodingTurnSettings
from app.tasks.models import RunTaskRequest, StartTaskRequest
from app.tasks.service import (
    TaskOperationError,
    accept_task,
    reject_task,
    run_task,
    start_task,
    verify_task,
)


@dataclass(frozen=True)
class ChangeClaim:
    request_id: str
    delegation_id: str
    session_id: str
    sandbox_id: str
    task_id: str
    volume_name: str
    instructions: str
    prompt: str
    provider: Any
    model: str
    turn_settings: CodingTurnSettings
    verification: list[ResolvedVerification]


def claim_change_request(
    docker_client: DockerClient,
    settings: CodingTurnSettings,
    store: ControllerStore,
    delegation_id: str,
    request: RequestFeatureChange,
    *,
    session_id: str,
    project_name: str,
) -> ChangeClaim:
    view = service.view(
        store,
        delegation_id,
        session_id=session_id,
        project_name=project_name,
    )
    if view.delegation.status is not DelegationStatus.COMPLETED:
        raise service.DelegationOperationError(
            409,
            "Changes can be requested only after all work items are complete",
        )
    if view.review and view.review.source_merged_at:
        raise service.DelegationOperationError(
            409,
            "The implementation is already merged into the project folder",
        )
    if any(change.status is ChangeRequestStatus.RUNNING for change in view.changes):
        raise service.DelegationOperationError(409, "Another change request is running")
    instructions = request.instructions.strip()
    if not instructions:
        raise service.DelegationOperationError(422, "Change instructions cannot be empty")

    context_id = view.delegation.context_id
    try:
        context = get_context(
            store,
            context_id or "",
            session_id=session_id,
            project_name=project_name,
        )
    except ContextOperationError as error:
        raise service.DelegationOperationError(error.status_code, error.detail) from error
    verification = [
        ResolvedVerification(
            command_kind=command.kind,
            command=command.command,
            reason="Verify the complete implementation after requested changes",
        )
        for command in context.commands
        if command.confirmed
    ]
    if not verification:
        raise service.DelegationOperationError(
            409,
            "The implementation context has no confirmed verification commands",
        )
    session = store.planning_session(view.delegation.session_id)
    if session is None:
        raise service.DelegationOperationError(404, "Planning session was not found")
    feature_context = _feature_context(
        json_object(session.get("plan_spec_json")) or {},
        context.manifest.model_dump(mode="json") if context.manifest else {},
        view,
    )
    prompt = _prompt(instructions, feature_context)
    sandbox = store.sandbox(view.delegation.sandbox_id)
    if sandbox is None:
        raise service.DelegationOperationError(404, "Delegation sandbox was not found")

    try:
        task = start_task(
            docker_client,
            store,
            StartTaskRequest(project_name=project_name, title="Requested feature changes"),
        )
    except TaskOperationError as error:
        raise service.DelegationOperationError(error.status_code, error.detail) from error

    request_id = uuid4().hex
    model = request.model or settings.model(request.provider.value)
    try:
        store.claim_delegation_change_request(
            {
                "id": request_id,
                "delegation_id": delegation_id,
                "status": ChangeRequestStatus.RUNNING.value,
                "instructions": instructions,
                "provider": request.provider.value,
                "model": model,
                "task_id": task.id,
                "prompt": prompt,
            }
        )
    except RevisionTaken as error:
        _discard_task(docker_client, store, task.id)
        raise service.DelegationOperationError(
            409,
            "This change request revision was claimed concurrently",
        ) from error
    except ChangeRequestRunning as error:
        _discard_task(docker_client, store, task.id)
        raise service.DelegationOperationError(
            409,
            "Another change request is running",
        ) from error

    _progress(
        store,
        request_id,
        view.delegation.sandbox_id,
        step="claimed",
        message=f"Change request reserved for {request.provider.value}/{model}",
    )
    return ChangeClaim(
        request_id=request_id,
        delegation_id=delegation_id,
        session_id=session_id,
        sandbox_id=view.delegation.sandbox_id,
        task_id=task.id,
        volume_name=str(sandbox["volume_name"]),
        instructions=instructions,
        prompt=prompt,
        provider=request.provider,
        model=model,
        turn_settings=replace(settings, credential_profile=request.credential_profile),
        verification=verification,
    )


def execute_change_request(
    docker_client: DockerClient,
    store: ControllerStore,
    claim: ChangeClaim,
) -> FeatureChangeRequest:
    try:
        _progress(
            store,
            claim.request_id,
            claim.sandbox_id,
            step="turn",
            message=f"Applying requested changes with {claim.provider.value}/{claim.model}",
        )
        response = run_task(
            docker_client,
            store,
            claim.turn_settings,
            claim.task_id,
            RunTaskRequest(
                prompt=claim.prompt,
                provider=claim.provider,
                model=claim.model,
            ),
        )
        if not response.committed:
            raise service.DelegationOperationError(
                409,
                response.turn_error or response.detail or "The agent made no committed changes",
            )

        _progress(
            store,
            claim.request_id,
            claim.sandbox_id,
            step="verification",
            message="Verifying the complete updated implementation",
        )
        verification = run_verification(
            docker_client,
            get_verification_settings(),
            volume_name=claim.volume_name,
            commands=claim.verification,
            controller_store=store,
            sandbox_id=claim.sandbox_id,
        )
        if not verification["passed"]:
            raise service.DelegationOperationError(
                409,
                _verification_failure(verification),
            )
        verification["agent_report"] = response.result
        verification["acceptance_evidence"] = _acceptance_evidence(response.result)
        verification["turn"] = {
            "model": response.model,
            "duration_ms": response.duration_ms,
            "exit_code": response.exit_code,
            "tool_calls": response.tool_calls,
            "failed_tool_calls": response.failed_tool_calls,
            "usage": response.usage.model_dump(mode="json"),
        }
        verify_task(
            store,
            claim.task_id,
            verification_passed=True,
            detail="Full implementation verification passed",
        )
        accept_task(docker_client, store, claim.task_id)
        row = store.settle_delegation_change_request(
            claim.request_id,
            to_status=ChangeRequestStatus.AWAITING_REVIEW.value,
            verification_json=json.dumps(verification),
        )
        if row is None:
            raise service.DelegationOperationError(409, "Change request was settled elsewhere")
    except Exception as error:
        detail = str(getattr(error, "detail", error)) or "Change request failed"
        _discard_task(docker_client, store, claim.task_id)
        store.settle_delegation_change_request(
            claim.request_id,
            to_status=ChangeRequestStatus.FAILED.value,
            error=detail[:2000],
        )
        _progress(
            store,
            claim.request_id,
            claim.sandbox_id,
            step="failed",
            message=detail,
            level="error",
        )
        raise

    _progress(
        store,
        claim.request_id,
        claim.sandbox_id,
        step="settled",
        message="Requested changes joined the implementation and await whole-feature review",
    )
    return _change_from_row(row)


def fail_change_claim(store: ControllerStore, claim: ChangeClaim, detail: str) -> None:
    store.settle_delegation_change_request(
        claim.request_id,
        to_status=ChangeRequestStatus.FAILED.value,
        error=detail[:2000],
    )
    _progress(
        store,
        claim.request_id,
        claim.sandbox_id,
        step="failed",
        message=detail,
        level="error",
    )


def _prompt(instructions: str, feature_context: Mapping[str, Any]) -> str:
    return f"""Update the completed feature implementation from the user's instructions.

Work against the current repository state. Preserve the completed implementation.
Make only changes needed for this request. Inspect related code and tests before editing.
Do not remove working behavior unless the request requires it.

Translate the request into observable acceptance criteria before editing. For each
criterion, state the expected state, action, and result when those parts apply.
For timed behavior, include the exact timing requirement. For user-visible behavior,
inspect related styling and run a test that exercises the behavior. A build alone does
not verify an interaction. If suitable behavior verification is unavailable, report
that criterion as unverified instead of claiming completion.

The coding image already provides Playwright and Chromium. Use the global `playwright`
command or CommonJS `require("playwright")` from Node. Do not install Playwright,
Puppeteer, Chromium, Chrome, or other test infrastructure. Do not run the Playwright
browser installer, add verification-only packages, or change package manifests for
tooling.
If the provided browser cannot run, report the behavior as unverified and continue
without downloading a replacement.

## Feature context

{_bounded_json(feature_context)}

## Requested changes

{instructions}

## Required completion evidence

Include these fields in the final JSON object required by the completion contract:

"change_kind": "interactive_ui | api_behavior | data_behavior | static_code",
"acceptance_criteria": [
  {{
    "criterion": "observable result",
    "verification_kind": "behavior_test | static_check | manual_check",
    "verified": true,
    "evidence": "test or inspection evidence"
  }}
]

Set "verified" to false when evidence is missing. In "verification", use outcome
"passed" only when the listed commands or checks exercise the requested behavior.
Interactive UI, API behavior, and data behavior require at least one behavior_test
criterion and a behavior check. Build, lint, typecheck, and static inspection do not
satisfy that requirement.
"""


def _feature_context(
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    view: Any,
) -> dict[str, Any]:
    completed_work = [
        {
            "key": entry.item.key,
            "title": entry.item.title,
            "acceptance_criteria": entry.item.acceptance_criteria,
            "result": _compact_result(entry.runs[-1].result if entry.runs else None),
            "verification": _compact_verification(
                entry.runs[-1].verification if entry.runs else None
            ),
        }
        for entry in view.items
    ]
    previous_changes = [
        {
            "revision": change.revision,
            "status": change.status.value,
            "instructions": change.instructions,
            "verification": _compact_verification(change.verification),
        }
        for change in view.changes
    ]
    return {
        "reviewed_plan": dict(plan),
        "implementation_manifest": dict(manifest),
        "completed_work": completed_work,
        "previous_change_requests": previous_changes,
    }


def _compact_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    keys = ("changed", "decisions", "interfaces", "acceptance_criteria", "verification")
    return {key: value[key] for key in keys if key in value}


def _compact_verification(value: Any) -> dict[str, Any] | None:
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
    compact = {
        "passed": value.get("passed"),
        "commands": commands,
    }
    for key in ("acceptance_evidence", "agent_report", "turn"):
        if key in value:
            compact[key] = value[key]
    return compact


def _acceptance_evidence(result: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(result, Mapping):
        return {"complete": False, "errors": ["Agent returned no structured report"]}

    change_kind = result.get("change_kind")
    behavioral_change = change_kind in {
        "interactive_ui",
        "api_behavior",
        "data_behavior",
    }
    if change_kind not in {
        "interactive_ui",
        "api_behavior",
        "data_behavior",
        "static_code",
    }:
        errors.append("Agent reported no recognized change kind")

    criteria = result.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("Agent reported no observable acceptance criteria")
        criteria = []
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, Mapping):
            errors.append(f"acceptance_criteria[{index}] is not an object")
            continue
        if not isinstance(criterion.get("criterion"), str) or not str(
            criterion.get("criterion")
        ).strip():
            errors.append(f"acceptance_criteria[{index}] has no criterion")
        if criterion.get("verified") is not True:
            errors.append(f"acceptance_criteria[{index}] is not verified")
        verification_kind = criterion.get("verification_kind")
        if verification_kind not in {"behavior_test", "static_check", "manual_check"}:
            errors.append(
                f"acceptance_criteria[{index}] has no recognized verification kind"
            )
        if behavioral_change and verification_kind != "behavior_test":
            errors.append(
                f"acceptance_criteria[{index}] lacks a behavioral test"
            )
        if not isinstance(criterion.get("evidence"), str) or not str(
            criterion.get("evidence")
        ).strip():
            errors.append(f"acceptance_criteria[{index}] has no evidence")

    verification = result.get("verification")
    if not isinstance(verification, Mapping):
        errors.append("Agent reported no structured verification")
    else:
        ran = verification.get("ran")
        if not isinstance(ran, list) or not ran:
            errors.append("Agent reported no verification commands or checks")
        else:
            checks = [check for check in ran if isinstance(check, str)]
            if any(_installs_test_infrastructure(check) for check in checks):
                errors.append("Agent attempted to install test infrastructure")
            if behavioral_change and not any(_is_behavior_check(check) for check in checks):
                errors.append("Behavioral change has no behavioral verification check")
        if verification.get("outcome") != "passed":
            errors.append("Agent did not report a passed verification outcome")

    return {
        "complete": not errors,
        "errors": errors,
        "criteria": criteria,
        "change_kind": change_kind,
    }


def _is_behavior_check(check: str) -> bool:
    normalized = check.casefold()
    if _installs_test_infrastructure(normalized):
        return False
    return any(
        marker in normalized
        for marker in (
            " test",
            "test ",
            "pytest",
            "vitest",
            "jest",
            "playwright",
            "cypress",
            "browser",
            "click",
            "interaction",
            "request",
            "response",
        )
    )


# Match an install the way it is written as a command, not as the bare word.
# The old bare " install" matched prose such as "no install of any kind", so an
# agent that correctly installed nothing was recorded as having installed test
# infrastructure, and its real browser run stopped counting as a behaviour
# check. Both errors came from that one substring.
_INSTALL_ACTION = re.compile(
    r"\b(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:install|add|i)\b"
    r"|(?:npx\s+)?(?:playwright|puppeteer)\s+install\b"
    r"|apt(?:-get)?\s+install\b"
    r"|apk\s+add\b"
    r"|(?:pip|pip3)\s+install\b"
    r"|brew\s+install\b"
    r")"
)
# Prose that denies the install sitting right after it.
_INSTALL_NEGATION = re.compile(
    r"\b(?:no|not|never|without|avoided?|skipped?|"
    r"don'?t|doesn'?t|didn'?t|did\s+not|does\s+not|do\s+not)\b"
)
#: How far back a denial can sit and still govern the install it denies.
_NEGATION_WINDOW = 40


def _installs_test_infrastructure(command: str) -> bool:
    normalized = command.casefold()
    browser_tool = any(
        tool in normalized
        for tool in ("playwright", "puppeteer", "chromium", "google-chrome")
    )
    if not browser_tool:
        return False
    for match in _INSTALL_ACTION.finditer(normalized):
        preceding = normalized[max(0, match.start() - _NEGATION_WINDOW) : match.start()]
        if not _INSTALL_NEGATION.search(preceding):
            return True
    return False


def _bounded_json(value: Mapping[str, Any], limit: int = 120_000) -> str:
    rendered = json.dumps(value, indent=2, default=str)
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[:limit]}\n[feature context truncated at {limit} characters]"


def _verification_failure(verification: dict[str, Any]) -> str:
    failed = next(
        (command for command in verification.get("commands", []) if not command.get("passed")),
        None,
    )
    if failed:
        return f"Verification failed: {failed.get('command')} ({failed.get('detail')})"
    return "Full implementation verification failed"


def _discard_task(docker_client: DockerClient, store: ControllerStore, task_id: str) -> None:
    try:
        reject_task(docker_client, store, task_id)
    except TaskOperationError:
        pass


def _progress(
    store: ControllerStore,
    request_id: str,
    sandbox_id: str,
    *,
    step: str,
    message: str,
    level: str = "info",
) -> None:
    store.progress_event(
        sandbox_id=sandbox_id,
        run_id=request_id,
        kind="change.progress",
        step=step,
        message=message,
        level=level,
    )


def _change_from_row(row: dict[str, Any]) -> FeatureChangeRequest:
    return FeatureChangeRequest(
        id=str(row["id"]),
        delegation_id=str(row["delegation_id"]),
        revision=int(row["revision"]),
        status=str(row["status"]),
        instructions=str(row["instructions"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        task_id=row.get("task_id"),
        verification=json.loads(str(row["verification_json"]))
        if row.get("verification_json")
        else None,
        error=row.get("error"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        settled_at=row.get("settled_at"),
    )


__all__ = [
    "ChangeClaim",
    "claim_change_request",
    "execute_change_request",
    "fail_change_claim",
]
