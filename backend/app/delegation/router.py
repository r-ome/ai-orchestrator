from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from docker.client import DockerClient
from docker.errors import APIError, ContainerError, DockerException, NotFound
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app import jobs
from app.agents.service import AgentOperationError
from app.controller.store import ControllerStore, get_controller_store
from app.delegation.config import (
    DelegatorSettings,
    IntegrationReviewSettings,
    get_delegator_settings,
    get_integration_review_settings,
    get_verification_settings,
)
from app.delegation.change_requests import (
    claim_change_request,
    execute_change_request,
    fail_change_claim,
)
from app.delegation.delivery import feature_diff, merge_feature_to_source
from app.delegation.execution import (
    accept_run,
    build_run_packet,
    claim_run,
    execute_run,
    fail_run_claim,
    reject_run,
)
from app.delegation.integration_review import (
    claim_integration_review,
    execute_integration_review,
    fail_review_claim,
)
from app.delegation.models import (
    AcceptedJob,
    DelegationsResponse,
    DelegationStatus,
    DelegationView,
    FeatureDiff,
    GenerateDelegationRequest,
    GenerateIntegrationReviewRequest,
    MergeFeatureOutcome,
    MergeFeatureRequest,
    RequestFeatureChange,
    SetRoutingRequest,
    StartRunOutcome,
    StartRunRequest,
)
from app.delegation.packet import Packet
from app.delegation.verification import VerificationSettings
from app.delegation.service import (
    DelegationOperationError,
    claim_generation,
    create_revision,
    clear_routing,
    execute_generation,
    fail_generation_claim,
    list_delegations,
    set_routing,
    transition,
    view,
)
from app.docker_client import get_docker_client
from app.planning.config import PlanningSettings, get_planning_settings
from app.planning.runner import PlanningTurnError
from app.previews.config import PreviewSettings, get_preview_settings
from app.projects.config import ProjectSettings, get_project_settings
from app.tasks.config import CodingTurnSettings, get_coding_turn_settings
from app.tasks.runner import CodingTurnError


router = APIRouter(
    prefix="/projects/{project_name}/planning/sessions/{session_id}/delegations",
    tags=["delegations"],
)
ResponseType = TypeVar("ResponseType")
StoreDep = Annotated[ControllerStore, Depends(get_controller_store)]


def _response(function: Callable[[], ResponseType]) -> ResponseType:
    try:
        return function()
    except (
        DelegationOperationError,
        PlanningTurnError,
        AgentOperationError,
        CodingTurnError,
    ) as error:
        raise HTTPException(error.status_code, error.detail) from error
    except NotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Docker resource not found") from error
    except ContainerError as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Delegation helper container failed: {error}",
        ) from error
    except APIError as error:
        response_status = getattr(getattr(error, "response", None), "status_code", 0)
        raise HTTPException(
            response_status or status.HTTP_502_BAD_GATEWAY,
            "Docker rejected the delegation operation",
        ) from error
    except DockerException as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Docker daemon is unavailable",
        ) from error


@router.post("", response_model=DelegationView, status_code=status.HTTP_201_CREATED)
def open_delegation(
    project_name: str,
    session_id: str,
    store: StoreDep,
    items: Annotated[list[dict[str, Any]], Body(embed=True)],
) -> DelegationView:
    return _response(
        lambda: create_revision(
            store,
            session_id,
            items,
            project_name=project_name,
        )
    )


@router.post(
    "/generate",
    response_model=AcceptedJob,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_delegation(
    project_name: str,
    session_id: str,
    request: GenerateDelegationRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    planning_settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
    settings: Annotated[DelegatorSettings, Depends(get_delegator_settings)],
    store: StoreDep,
) -> AcceptedJob:
    """Claim a decomposition and run its turn in the background. See create_context.

    `docker_client` is unused on purpose: resolving the dependency answers 503
    while Docker is unreachable, so a row is never claimed for a turn that
    cannot start. The job builds its own client — this one is closed when the
    response is sent.
    """
    claim = _response(
        lambda: claim_generation(
            planning_settings,
            settings,
            store,
            session_id,
            request,
            project_name=project_name,
        )
    )
    jobs.submit_docker_job(
        lambda client: execute_generation(client, store, claim),
        name=f"delegation:{claim.job_id}",
        on_setup_error=lambda detail: fail_generation_claim(store, claim, detail),
    )
    return AcceptedJob(
        job_id=claim.job_id,
        kind="delegation",
        detail="Decomposition is running",
    )


@router.get("", response_model=DelegationsResponse)
def get_delegations(
    project_name: str,
    session_id: str,
    store: StoreDep,
) -> DelegationsResponse:
    delegations = _response(
        lambda: list_delegations(
            store,
            session_id,
            project_name=project_name,
        )
    )
    return DelegationsResponse(count=len(delegations), delegations=delegations)


@router.get("/{delegation_id}", response_model=DelegationView)
def read_delegation(
    project_name: str,
    session_id: str,
    delegation_id: str,
    store: StoreDep,
) -> DelegationView:
    return _response(
        lambda: view(
            store,
            delegation_id,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.post("/{delegation_id}/start", response_model=DelegationView)
def start_delegation(
    project_name: str,
    session_id: str,
    delegation_id: str,
    store: StoreDep,
) -> DelegationView:
    return _response(
        lambda: transition(
            store,
            delegation_id,
            DelegationStatus.RUNNING,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.post("/{delegation_id}/halt", response_model=DelegationView)
def halt_delegation(
    project_name: str,
    session_id: str,
    delegation_id: str,
    store: StoreDep,
    reason: Annotated[str, Body(embed=True)] = "",
) -> DelegationView:
    return _response(
        lambda: transition(
            store,
            delegation_id,
            DelegationStatus.HALTED,
            error=reason or None,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.post("/{delegation_id}/abandon", response_model=DelegationView)
def abandon_delegation(
    project_name: str,
    session_id: str,
    delegation_id: str,
    store: StoreDep,
) -> DelegationView:
    return _response(
        lambda: transition(
            store,
            delegation_id,
            DelegationStatus.ABANDONED,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.get("/{delegation_id}/items/{key}/packet", response_model=Packet)
def read_packet(
    project_name: str,
    session_id: str,
    delegation_id: str,
    key: str,
    store: StoreDep,
) -> Packet:
    return _response(
        lambda: build_run_packet(
            store,
            delegation_id,
            key,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.post(
    "/{delegation_id}/items/{key}/run",
    response_model=AcceptedJob,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_work_item(
    project_name: str,
    session_id: str,
    delegation_id: str,
    key: str,
    request: StartRunRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[CodingTurnSettings, Depends(get_coding_turn_settings)],
    verification_settings: Annotated[
        VerificationSettings,
        Depends(get_verification_settings),
    ],
    store: StoreDep,
) -> AcceptedJob:
    """Claim a work item and run its coding turn in the background.

    This is the endpoint that made a browser report a network error: a coding
    turn waits up to CODING_TURN_TIMEOUT_SECONDS (1800) and runs twice on a
    provider failure, so the response could arrive an hour after the request.
    """
    claim = _response(
        lambda: claim_run(
            docker_client,
            settings,
            store,
            delegation_id,
            key,
            request,
            session_id=session_id,
            project_name=project_name,
        )
    )
    jobs.submit_docker_job(
        lambda client: execute_run(
            client,
            store,
            claim,
            verification_settings=verification_settings,
        ),
        name=f"run:{claim.run_id}",
        on_setup_error=lambda detail: fail_run_claim(store, claim, detail),
    )
    return AcceptedJob(
        job_id=claim.run_id,
        kind="run",
        detail=(
            f"Work item '{key}' is running on "
            f"{claim.decision.provider.value}/{claim.decision.model}"
        ),
    )


@router.post("/{delegation_id}/runs/{run_id}/accept", response_model=StartRunOutcome)
def accept_work_item_run(
    project_name: str,
    session_id: str,
    delegation_id: str,
    run_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    store: StoreDep,
) -> StartRunOutcome:
    return _response(
        lambda: accept_run(
            docker_client,
            store,
            delegation_id,
            run_id,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.post("/{delegation_id}/runs/{run_id}/reject", response_model=StartRunOutcome)
def reject_work_item_run(
    project_name: str,
    session_id: str,
    delegation_id: str,
    run_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    store: StoreDep,
    reason: Annotated[str, Body(embed=True)] = "",
) -> StartRunOutcome:
    return _response(
        lambda: reject_run(
            docker_client,
            store,
            delegation_id,
            run_id,
            reason,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.put("/{delegation_id}/items/{key}/routing", response_model=DelegationView)
def set_item_routing(
    project_name: str,
    session_id: str,
    delegation_id: str,
    key: str,
    request: SetRoutingRequest,
    store: StoreDep,
) -> DelegationView:
    return _response(
        lambda: set_routing(
            store,
            delegation_id,
            key,
            request,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.delete("/{delegation_id}/items/{key}/routing", response_model=DelegationView)
def clear_item_routing(
    project_name: str,
    session_id: str,
    delegation_id: str,
    key: str,
    store: StoreDep,
) -> DelegationView:
    return _response(
        lambda: clear_routing(
            store,
            delegation_id,
            key,
            session_id=session_id,
            project_name=project_name,
        )
    )


@router.post(
    "/{delegation_id}/review",
    response_model=AcceptedJob,
    status_code=status.HTTP_202_ACCEPTED,
)
def review_delegation(
    project_name: str,
    session_id: str,
    delegation_id: str,
    request: GenerateIntegrationReviewRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    planning_settings: Annotated[PlanningSettings, Depends(get_planning_settings)],
    settings: Annotated[
        IntegrationReviewSettings,
        Depends(get_integration_review_settings),
    ],
    store: StoreDep,
) -> AcceptedJob:
    """Claim a feature review and run its turn in the background.

    `docker_client` is unused here for the same reason as in
    `generate_delegation`: it is the readiness check, not the job's client.
    """
    claim = _response(
        lambda: claim_integration_review(
            planning_settings,
            settings,
            store,
            delegation_id,
            request,
            session_id=session_id,
            project_name=project_name,
        )
    )
    jobs.submit_docker_job(
        lambda client: execute_integration_review(client, store, claim),
        name=f"review:{claim.review_id}",
        on_setup_error=lambda detail: fail_review_claim(store, claim, detail),
    )
    return AcceptedJob(
        job_id=claim.review_id,
        kind="review",
        detail="Feature review is running",
    )


@router.post(
    "/{delegation_id}/changes",
    response_model=AcceptedJob,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_feature_changes(
    project_name: str,
    session_id: str,
    delegation_id: str,
    request: RequestFeatureChange,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[CodingTurnSettings, Depends(get_coding_turn_settings)],
    store: StoreDep,
) -> AcceptedJob:
    claim = _response(
        lambda: claim_change_request(
            docker_client,
            settings,
            store,
            delegation_id,
            request,
            session_id=session_id,
            project_name=project_name,
        )
    )
    jobs.submit_docker_job(
        lambda client: execute_change_request(client, store, claim),
        name=f"change:{claim.request_id}",
        on_setup_error=lambda detail: fail_change_claim(store, claim, detail),
    )
    return AcceptedJob(
        job_id=claim.request_id,
        kind="change",
        detail="Requested feature changes are running",
    )


@router.get("/{delegation_id}/diff", response_model=FeatureDiff)
def read_feature_diff(
    project_name: str,
    session_id: str,
    delegation_id: str,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
    store: StoreDep,
) -> FeatureDiff:
    return _response(
        lambda: feature_diff(
            docker_client,
            settings,
            store,
            view(
                store,
                delegation_id,
                session_id=session_id,
                project_name=project_name,
            ),
        )
    )


@router.post("/{delegation_id}/merge", response_model=MergeFeatureOutcome)
def merge_feature(
    project_name: str,
    session_id: str,
    delegation_id: str,
    request: MergeFeatureRequest,
    docker_client: Annotated[DockerClient, Depends(get_docker_client)],
    preview_settings: Annotated[PreviewSettings, Depends(get_preview_settings)],
    project_settings: Annotated[ProjectSettings, Depends(get_project_settings)],
    store: StoreDep,
) -> MergeFeatureOutcome:
    return _response(
        lambda: merge_feature_to_source(
            docker_client,
            preview_settings,
            project_settings,
            store,
            view(
                store,
                delegation_id,
                session_id=session_id,
                project_name=project_name,
            ),
            request,
        )
    )
