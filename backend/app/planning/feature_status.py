from app.planning.models import FeatureStatus, PlanningStatus


def derive_feature_status(
    planning_status: PlanningStatus,
    *,
    context_status: str | None,
    delegation_status: str | None,
    review_status: str | None,
    review_approved: bool | None,
    source_merged_at: str | None,
    change_status: str | None,
    pr_number: int | None,
    pr_state: str | None,
    pr_merged_at: str | None,
) -> FeatureStatus:
    """Derive the feature lifecycle label from planning and downstream facts."""
    if planning_status not in {
        PlanningStatus.PLAN_READY,
        PlanningStatus.REVIEW_LIMIT_REACHED,
    }:
        return FeatureStatus(planning_status)

    # A pull request carries commits, and commits come from a delegation. A
    # session that never delegated cannot have published anything, so PR facts
    # that reach it describe somebody else's work and are dropped here.
    #
    # This is belt to the join's braces: `sandbox_publications` is now scoped
    # to a session, but the row is still keyed by sandbox, so a publication
    # attributed to the wrong session — or to none — must not be able to
    # promote an empty session to `published` or `merged`.
    if delegation_status is None:
        pr_number = None
        pr_merged_at = None

    normalised_pr_state = str(pr_state or "").lower()

    # A recorded merge is final even when older rows still report another stage.
    if pr_merged_at is not None or source_merged_at is not None:
        return FeatureStatus.MERGED

    # An open pull request makes the feature published.
    if pr_number is not None and normalised_pr_state == "open":
        return FeatureStatus.PUBLISHED

    # An abandoned delegation stops the feature without a later outcome.
    if delegation_status == "abandoned":
        return FeatureStatus.ABANDONED

    # Failures override in-progress labels so the problem stays visible.
    if (
        context_status == "failed"
        or delegation_status == "halted"
        or review_status == "failed"
        or change_status == "failed"
    ):
        return FeatureStatus.BLOCKED

    # An approved latest review is later than the review stage itself.
    if review_status == "completed" and review_approved is True and pr_number is None:
        return FeatureStatus.APPROVED

    # Completed work or active review work waits for a review outcome.
    if (
        delegation_status == "completed"
        or review_status == "generating"
        or (review_status == "completed" and review_approved is False)
        or change_status in {"running", "awaiting_review"}
    ):
        return FeatureStatus.IN_REVIEW

    # A delegation can run before its optional context row exists.
    if context_status in {"generating", "ready"} or delegation_status in {
        "ready",
        "running",
    }:
        return FeatureStatus.BUILDING

    if all(
        value is None
        for value in (
            context_status,
            delegation_status,
            review_status,
            change_status,
            pr_number,
        )
    ):
        return FeatureStatus(planning_status)

    # Unanticipated downstream facts still show work in progress, never an unstarted plan.
    if review_status is not None or change_status is not None:
        return FeatureStatus.IN_REVIEW
    return FeatureStatus.BUILDING
