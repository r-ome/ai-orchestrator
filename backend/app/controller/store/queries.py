_PLANNING_SESSION_FEATURE_FACTS_QUERY = """
SELECT
    session.*,
    context.status AS context_status,
    delegation.status AS delegation_status,
    review.status AS review_status,
    review.result_json AS review_result_json,
    review.source_merged_at AS review_source_merged_at,
    change_request.status AS change_status,
    publication.pr_number AS pr_number,
    publication.pr_state AS pr_state,
    publication.pr_merged_at AS pr_merged_at
FROM planning_sessions AS session
LEFT JOIN implementation_contexts AS context
    ON context.session_id = session.id
LEFT JOIN delegations AS delegation
    ON delegation.session_id = session.id
    AND delegation.revision = (
        SELECT MAX(revision)
        FROM delegations
        WHERE session_id = session.id
    )
LEFT JOIN delegation_reviews AS review
    ON review.delegation_id = delegation.id
    AND review.revision = (
        SELECT MAX(revision)
        FROM delegation_reviews
        WHERE delegation_id = delegation.id
    )
LEFT JOIN delegation_change_requests AS change_request
    ON change_request.delegation_id = delegation.id
    AND change_request.revision = (
        SELECT MAX(revision)
        FROM delegation_change_requests
        WHERE delegation_id = delegation.id
    )
LEFT JOIN sandbox_publications AS publication
    ON publication.sandbox_id = session.sandbox_id
    -- Every other join above is scoped to this session. This one was scoped to
    -- the sandbox alone, so a sandbox holding two planning sessions gave both
    -- of them the same pull request, and a session that had produced nothing
    -- reported itself as published. A publication with no owner attributes to
    -- nobody, which under-claims instead of claiming somebody else's work.
    AND publication.session_id = session.id
"""
