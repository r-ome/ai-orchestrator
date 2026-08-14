import pytest

from app.planning.feature_status import derive_feature_status
from app.planning.models import FeatureStatus, PlanningStatus


def _derive(**overrides: object) -> FeatureStatus:
    values: dict[str, object] = {
        "context_status": None,
        "delegation_status": None,
        "review_status": None,
        "review_approved": None,
        "source_merged_at": None,
        "change_status": None,
        "pr_number": None,
        "pr_state": None,
        "pr_merged_at": None,
    }
    values.update(overrides)
    return derive_feature_status(PlanningStatus.PLAN_READY, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # A pull request implies a delegation produced the commits in it, so
        # every PR case here carries one. See
        # `test_a_pull_request_without_a_delegation_belongs_to_another_session`.
        (
            {
                "delegation_status": "completed",
                "pr_merged_at": "2026-08-14T00:00:00Z",
            },
            FeatureStatus.MERGED,
        ),
        (
            {"delegation_status": "completed", "pr_number": 42, "pr_state": "open"},
            FeatureStatus.PUBLISHED,
        ),
        ({"delegation_status": "abandoned"}, FeatureStatus.ABANDONED),
        ({"context_status": "failed"}, FeatureStatus.BLOCKED),
        (
            {"review_status": "completed", "review_approved": True},
            FeatureStatus.APPROVED,
        ),
        ({"delegation_status": "completed"}, FeatureStatus.IN_REVIEW),
        (
            {"context_status": "ready", "delegation_status": "running"},
            FeatureStatus.BUILDING,
        ),
        ({}, FeatureStatus.PLAN_READY),
    ],
)
def test_derives_each_feature_status_branch(
    overrides: dict[str, object], expected: FeatureStatus
) -> None:
    assert _derive(**overrides) is expected


@pytest.mark.parametrize(
    "planning_status",
    [
        PlanningStatus.CLARIFYING,
        PlanningStatus.AWAITING_CONFIRMATION,
        PlanningStatus.PLANNING,
        PlanningStatus.UNDER_REVIEW,
        PlanningStatus.FAILED,
        PlanningStatus.CANCELLED,
    ],
)
def test_pre_plan_ready_planning_statuses_pass_through(
    planning_status: PlanningStatus,
) -> None:
    assert (
        derive_feature_status(
            planning_status,
            context_status="failed",
            delegation_status="completed",
            review_status="completed",
            review_approved=True,
            source_merged_at="2026-08-14T00:00:00Z",
            change_status="failed",
            pr_number=42,
            pr_state="closed",
            pr_merged_at="2026-08-14T00:00:00Z",
        )
        is FeatureStatus(planning_status)
    )


def test_review_limit_reached_stays_until_downstream_work_starts() -> None:
    no_downstream = derive_feature_status(
        PlanningStatus.REVIEW_LIMIT_REACHED,
        context_status=None,
        delegation_status=None,
        review_status=None,
        review_approved=None,
        source_merged_at=None,
        change_status=None,
        pr_number=None,
        pr_state=None,
        pr_merged_at=None,
    )
    building = derive_feature_status(
        PlanningStatus.REVIEW_LIMIT_REACHED,
        context_status="ready",
        delegation_status="running",
        review_status=None,
        review_approved=None,
        source_merged_at=None,
        change_status=None,
        pr_number=None,
        pr_state=None,
        pr_merged_at=None,
    )

    assert no_downstream is FeatureStatus.REVIEW_LIMIT_REACHED
    assert building is FeatureStatus.BUILDING


def test_blocked_wins_over_in_review() -> None:
    assert _derive(review_status="failed", change_status="running") is FeatureStatus.BLOCKED


def test_merged_wins_over_published() -> None:
    assert _derive(
        delegation_status="completed",
        pr_number=42,
        pr_state="closed",
        pr_merged_at="2026-08-14T00:00:00Z",
    ) is FeatureStatus.MERGED


def test_merged_pull_request_never_reads_as_plan_ready() -> None:
    status = _derive(
        delegation_status="completed",
        pr_number=42,
        pr_state="closed",
        pr_merged_at="2026-08-14T00:00:00Z",
    )

    assert status is FeatureStatus.MERGED
    assert status is not FeatureStatus.PLAN_READY


def test_closed_unmerged_pull_request_stays_in_progress() -> None:
    status = _derive(
        delegation_status="completed",
        pr_number=42,
        pr_state="closed",
        review_status="completed",
        review_approved=True,
    )

    assert status is FeatureStatus.IN_REVIEW
    assert status is not FeatureStatus.PLAN_READY


def test_uppercase_open_pull_request_is_published() -> None:
    assert (
        _derive(delegation_status="completed", pr_number=42, pr_state="OPEN")
        is FeatureStatus.PUBLISHED
    )


def test_a_pull_request_without_a_delegation_belongs_to_another_session() -> None:
    """Two planning sessions in one sandbox shared a publication row.

    The sandbox held an open PR for the session that had actually built
    something. The other session had hit its review limit without producing a
    plan, and reported itself `published` — the most misleading label available
    for a session with no commits at all.
    """
    stuck = derive_feature_status(
        PlanningStatus.REVIEW_LIMIT_REACHED,
        context_status=None,
        delegation_status=None,
        review_status=None,
        review_approved=None,
        source_merged_at=None,
        change_status=None,
        pr_number=1,
        pr_state="open",
        pr_merged_at=None,
    )

    assert stuck is FeatureStatus.REVIEW_LIMIT_REACHED


def test_a_merged_pull_request_without_a_delegation_is_also_ignored() -> None:
    assert _derive(
        pr_number=1,
        pr_state="closed",
        pr_merged_at="2026-08-14T00:00:00Z",
    ) is FeatureStatus.PLAN_READY


def test_running_delegation_without_context_is_building() -> None:
    assert _derive(delegation_status="running") is FeatureStatus.BUILDING


def test_unanticipated_delegation_status_stays_building() -> None:
    status = _derive(context_status="legacy", delegation_status="unknown")

    assert status is FeatureStatus.BUILDING
    assert status is not FeatureStatus.PLAN_READY
