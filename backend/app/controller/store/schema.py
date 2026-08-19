INITIAL_MIGRATION = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sandboxes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    project_name TEXT NOT NULL,
    volume_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    baseline_commit TEXT,
    -- Versioned Git status, file type, and content fingerprints for paths that
    -- were already dirty before the first delegated task changed the sandbox.
    dirty_baseline_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    container_id TEXT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_agent_per_sandbox
ON agent_runs(sandbox_id)
WHERE status IN ('created', 'running', 'replacing', 'stopping');

CREATE TABLE IF NOT EXISTS preview_runs (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    proposal_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'live',
    task_id TEXT,
    commit_sha TEXT,
    status TEXT NOT NULL,
    selected_service TEXT,
    container_port INTEGER NOT NULL,
    host_port INTEGER,
    config_json TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    network_name TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    stopped_at TEXT,
    expires_at TEXT,
    last_activity_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_preview_per_sandbox
ON preview_runs(sandbox_id)
WHERE status IN ('preparing', 'running', 'restarting', 'rebuilding', 'stopping');

CREATE TABLE IF NOT EXISTS assigned_ports (
    host_port INTEGER PRIMARY KEY,
    preview_run_id TEXT NOT NULL UNIQUE REFERENCES preview_runs(id),
    assigned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_rounds (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    proposal_digest TEXT NOT NULL,
    detected_mode TEXT NOT NULL,
    config_json TEXT NOT NULL,
    protected_files_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    review_round_id TEXT NOT NULL REFERENCES review_rounds(id),
    proposal_digest TEXT NOT NULL,
    config_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    approved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protected_file_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    path TEXT NOT NULL,
    content BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    approval_id INTEGER REFERENCES approvals(id)
);

CREATE INDEX IF NOT EXISTS protected_baseline_lookup
ON protected_file_baselines(sandbox_id, path, id DESC);

-- One row per sandbox that holds credentials on a shared database server.
-- owner_sandbox_id is the sandbox whose schema this row points at. A row whose
-- owner is itself owns the data; any other row is a guest and must never drop it.
CREATE TABLE IF NOT EXISTS shared_database_schemas (
    sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id),
    project_id TEXT NOT NULL,
    owner_sandbox_id TEXT NOT NULL,
    sharing TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    user_name TEXT NOT NULL,
    image TEXT NOT NULL,
    persistence TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS shared_database_by_project
ON shared_database_schemas(project_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sandbox_id TEXT,
    run_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_secrets (
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    agent_run_id TEXT,
    branch TEXT NOT NULL,
    base_branch TEXT,
    base_commit TEXT NOT NULL,
    head_commit TEXT,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    -- Paths already dirty when the task branch was cut. A sandbox copied from
    -- a real repository arrives with untracked files the task never touches,
    -- and settlement must not read those as work the turn left uncommitted.
    baseline_dirty_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_sandbox
ON tasks(sandbox_id)
WHERE status IN ('open', 'reported', 'previewing', 'review');

CREATE TABLE IF NOT EXISTS planning_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    project_name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    turn_state TEXT NOT NULL DEFAULT 'idle',
    clarifier_provider TEXT NOT NULL,
    planner_provider TEXT NOT NULL,
    reviewer_provider TEXT NOT NULL,
    clarifier_model TEXT,
    planner_model TEXT,
    reviewer_model TEXT,
    reviewer_reasoning_effort TEXT,
    credential_profile TEXT NOT NULL DEFAULT 'default',
    max_review_turns INTEGER NOT NULL,
    review_turn INTEGER NOT NULL DEFAULT 0,
    plan_revision INTEGER NOT NULL DEFAULT 0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    understanding_summary TEXT NOT NULL DEFAULT '',
    feature_brief TEXT NOT NULL DEFAULT '',
    plan_spec_json TEXT,
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE INDEX IF NOT EXISTS planning_sessions_by_project
ON planning_sessions(project_id, created_at);

CREATE TABLE IF NOT EXISTS planning_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    raw_output TEXT NOT NULL DEFAULT '',
    revision INTEGER,
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS planning_findings (
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    finding_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    planner_response TEXT NOT NULL DEFAULT '',
    raised_in_round INTEGER NOT NULL,
    last_seen_round INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, finding_id)
);

CREATE TABLE IF NOT EXISTS planning_plan_revisions (
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    plan_markdown TEXT NOT NULL,
    reviewer_approved INTEGER,
    reviewer_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    PRIMARY KEY (session_id, revision)
);

-- Code-level knowledge a delegated run needs so it does not have to
-- rediscover the architecture. Points at code; never contains it.
--
-- One row per session, no revisions. The context is derived from a plan the
-- human and the model already agreed on, so choosing between derivations is a
-- decision that should not exist. Regenerating resets this row. What keeps a
-- running delegation's context from changing underneath it is not a revision
-- number but `claim_context`, which refuses to regenerate once the session has
-- a delegation.
CREATE TABLE IF NOT EXISTS implementation_contexts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    status TEXT NOT NULL,
    manifest_json TEXT,
    commands_json TEXT,
    inventory_json TEXT,
    provider TEXT,
    model TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_context_per_session
ON implementation_contexts(session_id);

-- One decomposition of a ready plan into work items, at a revision.
-- Revisions are added, never mutated: a completed run must keep pointing at
-- the definition it actually executed.
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES planning_sessions(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    context_id TEXT REFERENCES implementation_contexts(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_delegation_revision_per_session
ON delegations(session_id, revision);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_delegation_per_sandbox
ON delegations(sandbox_id)
WHERE status IN ('ready', 'running', 'halted');

CREATE TABLE IF NOT EXISTS delegation_reviews (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    base_branch TEXT,
    base_commit TEXT,
    head_commit TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    source_merged_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_review_revision_per_delegation
ON delegation_reviews(delegation_id, revision);

CREATE UNIQUE INDEX IF NOT EXISTS one_generating_review_per_delegation
ON delegation_reviews(delegation_id)
WHERE status = 'generating';

CREATE TABLE IF NOT EXISTS delegation_change_requests (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    instructions TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id),
    prompt TEXT,
    verification_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_change_revision_per_delegation
ON delegation_change_requests(delegation_id, revision);

CREATE UNIQUE INDEX IF NOT EXISTS one_running_change_per_delegation
ON delegation_change_requests(delegation_id)
WHERE status = 'running';

-- Immutable once its delegation revision exists. There is no update path on
-- purpose: changing a definition means a new revision. Carries no provider or
-- model, because what the work is and how it is run are different questions.
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    scope TEXT NOT NULL,
    out_of_scope TEXT NOT NULL DEFAULT '',
    dependencies_json TEXT NOT NULL,
    files_json TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    write_scope_json TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    complexity TEXT NOT NULL,
    architecture_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_work_item_key_per_delegation
ON work_items(delegation_id, key);

-- One attempt at a work item. Appended, never overwritten, so a retry does
-- not erase what the first attempt cost.
CREATE TABLE IF NOT EXISTS work_item_runs (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id),
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    routing_source TEXT,
    task_id TEXT REFERENCES tasks(id),
    result_json TEXT,
    failure_kind TEXT,
    error TEXT,
    verification_json TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    duration_ms INTEGER,
    exit_code INTEGER,
    repair_count INTEGER NOT NULL DEFAULT 0,
    -- Legacy runs can remain 'running' after their turn finishes while they
    -- wait for a decision. New delegated runs settle automatically after
    -- controller verification and an internal sandbox merge.
    turn_finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_number_per_work_item
ON work_item_runs(work_item_id, attempt);

-- Execution stays sequential. Eligibility is computed and shown in full; this
-- is what stops it being acted on concurrently.
CREATE UNIQUE INDEX IF NOT EXISTS one_running_run_per_delegation
ON work_item_runs(delegation_id)
WHERE status = 'running';

-- A person's routing choice for one work item. Separate from work_items
-- because a definition is immutable and an override is revisable.
CREATE TABLE IF NOT EXISTS work_item_routing (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    provider TEXT,
    model TEXT,
    actor TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


_CONTEXT_UPDATABLE_COLUMNS = frozenset(
    {
        "manifest_json",
        "commands_json",
        "inventory_json",
        "model",
        "error",
    }
)

_RUN_UPDATABLE_COLUMNS = frozenset(
    {
        "task_id",
        "model",
        "result_json",
        "failure_kind",
        "error",
        "verification_json",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "duration_ms",
        "exit_code",
        "repair_count",
        "routing_source",
    }
)


# Pre-squash controller databases carry the current effective schema and stamps
# for versions 1 through 17. Versions 2 through 17 stay reserved because a new
# migration there would be silently skipped during their upgrade.
FIRST_V1_MIGRATION = 18
