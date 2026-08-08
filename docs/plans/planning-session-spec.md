# Tech spec: project-level planning sessions

Implements `docs/adr/0004-controller-owned-planning-sessions.md`.
Behaviour and contracts live in `planning-session-design.md`.
Phase order and exit criteria live in `planning-session-plan.md`.

This spec is written to be executed by an agent that has not read the codebase.
Every anchor below was verified against the working tree on 2026-08-06. Line
numbers may drift; the function names are authoritative. Behaviour questions are
answered by the design spec, not by inventing an answer here.

---

## 1. Current behaviour

Read these before changing anything.

| Anchor | What it does |
|---|---|
| `backend/app/controller/store.py:13` `SCHEMA` | Every table, created with `CREATE TABLE IF NOT EXISTS` |
| `backend/app/controller/store.py:183` `initialize` | Runs `SCHEMA`, then applies `ALTER TABLE` migrations guarded by `"duplicate column name"`, then records versions 1–6 |
| `backend/app/controller/store.py:238` `_connection` | `RLock`-serialised connection, WAL, foreign keys on, transaction per call |
| `backend/app/controller/store.py:250` `register_sandbox` | Upserts the `projects` row then the `sandboxes` row. The only writer of either |
| `backend/app/controller/store.py:401` `create_task` | Insert plus `_event` in one transaction. Copy this shape |
| `backend/app/controller/store.py:484` `advance_task_status` | The guarded-UPDATE pattern: source statuses travel in the `WHERE`, returns whether the row moved |
| `backend/app/controller/store.py:981` `event`, `:1111` `_event` | The audit event store |
| `backend/app/controller/store.py:1147` `_row`, `:1151` `_json`, `:1155` `_now` | Row-to-dict, JSON dump, UTC ISO timestamp |
| `backend/app/tasks/models.py` `TASK_TRANSITIONS`, `source_statuses` | The status-machine pattern to copy exactly |
| `backend/app/agents/service.py:51` `WORKSPACE_DIRECTORY`, `:52` `CREDENTIAL_DIRECTORY` | `/workspace` and `/auth` |
| `backend/app/agents/service.py:71` `create_agent` | Derives `sandbox_id`, calls `register_sandbox`, then creates the container |
| `backend/app/agents/service.py:156` | The hardened `containers.create` call. Copy its flags |
| `backend/app/agents/service.py:437` `_credential_volume`, `:479` `_credential_volume_name` | Gets or creates the per-provider, per-profile credential volume |
| `backend/app/agents/config.py` `AgentSettings.provider` | Image, command, and credential env var per provider |
| `backend/app/projects/service.py:254` `inspect_registered_project` | Project lookup by name. Raises `ProjectOperationError` |
| `backend/app/projects/service.py:858` `ensure_git_baseline` | The throwaway hardened container pattern |
| `backend/app/projects/service.py:886` `_ensure_git_image` | Pull-if-absent helper |
| `backend/app/projects/service.py:912` `project_id` | Stable project id from a source path |
| `backend/app/tasks/router.py:26` `router`, `:29` `_docker_response` | Router and Docker-error-to-HTTP translation. Copy this |
| `backend/app/previews/router.py:51` | The `prefix="/projects/{project_name}"` router pattern |
| `backend/app/controller/lifecycle.py:27` `reconcile_controller_state` | Startup reconciliation, called from `main.py` `lifespan` |
| `backend/app/main.py` | Router registration and `lifespan` |
| `backend/tests/conftest.py` | Autouse fixture pointing every test at a `tmp_path` database |
| `backend/tests/tasks/test_service.py:34` `_StubContainers` | The stub Docker client pattern for service tests |
| `frontend/src/api/client.ts` `getJson`, `postJson` | The only HTTP helpers to use |
| `frontend/src/hooks/useApiResource.ts` `useApiResource` | Fetch, loading, error, reload. Keeps data during refresh |
| `frontend/src/components/ProjectAgentsSection.tsx` | The section-component pattern: `useApiResource`, a create form, `ConfirmDialog`, navigate on create |
| `frontend/src/pages/ProjectDetailPage.tsx:152` | Where sections are composed |
| `frontend/src/App.tsx:217` | The `/projects/:projectName/...` route pattern |

### Verified environment facts

Probed against the built images on 2026-08-06. Do not re-derive these.

- `orchestrator-agent-claude:latest` carries Claude Code `2.1.221`.
  `claude -p` prints and exits. It accepts `--output-format json`,
  `--permission-mode plan`, `--allowedTools`, `--model`, `--add-dir`,
  `--system-prompt`.
- `orchestrator-agent-codex:latest` carries `codex-cli 0.146.0`. `codex exec`
  accepts `--sandbox read-only`, `--ephemeral`, `--skip-git-repo-check`,
  `--output-last-message <FILE>`, `-C <DIR>`, `-m <MODEL>`, `-c key=value`.
- `codex exec` reads the prompt from stdin when no prompt argument is given, and
  **hangs forever if stdin is neither closed nor piped**. Every invocation must
  end with `< /dev/null`.
- Both images set the credential directory from an environment variable:
  `CLAUDE_CONFIG_DIR` for Claude, `CODEX_HOME` for Codex, both pointing at
  `/auth`. `codex` warns and continues if `/auth` does not exist.
- Planning containers need network access. Do **not** pass
  `network_disabled=True`; that flag belongs to the git and inspection helpers.

---

## 2. Phase 0 — Store schema, status machine, session models

**Owned files**

- `backend/app/controller/store.py` (edit)
- `backend/app/planning/__init__.py` (new)
- `backend/app/planning/models.py` (new)
- `backend/tests/planning/__init__.py` (new)
- `backend/tests/planning/test_models.py` (new)
- `backend/tests/planning/test_store.py` (new)
- `CONTEXT.md` (edit)

Do not create the router, service, or runner in this phase.

Add all ten terms from design spec section 1 to `CONTEXT.md`: Planning session,
Clarifier, Planner, Plan reviewer, Feature brief, Plan revision, Finding, Review
ledger, Plan Spec, Turn. Use that file's existing format: bold term, one-sentence
definition, and an `_Avoid_:` line where the design spec gives one. Note the
collision explicitly: `CONTEXT.md` already defines **Reviewer** as a read-only
sandbox participant, so the new entry is **Plan reviewer**.

### 2.1 Schema

Append to `SCHEMA` in `backend/app/controller/store.py`. Keep
`CREATE TABLE IF NOT EXISTS` so an existing database is unaffected.

```sql
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
```

In `initialize`, after the version-6 insert, add:

```python
connection.execute(
    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (7, ?)",
    (_now(),),
)
```

No `ALTER TABLE` is needed: these tables are new.

**Foreign keys matter here.** `planning_sessions.sandbox_id` references
`sandboxes(id)`, and `PRAGMA foreign_keys = ON` is set per connection
(`store.py:243`). A session cannot be created for a project with no sandbox row.
Section 4.2 makes the service register the sandbox first.

### 2.2 Status machine

`backend/app/planning/models.py`. Mirror `backend/app/tasks/models.py` exactly,
including the module docstring style and the `source_statuses` helper.

```python
class PlanningStatus(StrEnum):
    CLARIFYING = "clarifying"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PLANNING = "planning"
    UNDER_REVIEW = "under_review"
    PLAN_READY = "plan_ready"
    REVIEW_LIMIT_REACHED = "review_limit_reached"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanningTurnState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"


class PlanningRole(StrEnum):
    USER = "user"
    CLARIFIER = "clarifier"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    SYSTEM = "system"


class FindingStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    REJECTED = "rejected"
    RESOLVED = "resolved"
```

`PLANNING_TRANSITIONS` is the authority. Build it from the table in the design
spec section 2. `TERMINAL_PLANNING_STATUSES` is
`{PLAN_READY, REVIEW_LIMIT_REACHED, FAILED, CANCELLED}`. Add
`source_statuses(target)` identical in shape to the tasks version.

`CANCELLED` is a permitted target from every non-terminal status. Write that
into the table explicitly rather than special-casing it in the service, so a
test that walks the table proves it.

### 2.3 Pydantic models

Also in `backend/app/planning/models.py`:

```python
class CreatePlanningSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=8000)
    clarifier_provider: AgentProvider | None = None
    planner_provider: AgentProvider | None = None
    reviewer_provider: AgentProvider | None = None
    max_review_turns: int | None = Field(default=None, ge=1, le=10)


class PlanningMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class PlanningMessage(BaseModel):
    sequence: int
    role: PlanningRole
    text: str
    questions: list[str] = []
    revision: int | None = None
    created_at: str


class PlanningFinding(BaseModel):
    finding_id: str
    severity: str
    text: str
    status: FindingStatus
    planner_response: str = ""
    raised_in_round: int
    last_seen_round: int


class PlanComponent(BaseModel):
    name: str
    responsibility: str = ""


class PlanRisk(BaseModel):
    severity: str = "medium"
    text: str


class ReviewerOutcome(BaseModel):
    approved: bool
    rounds: int
    summary: str = ""
    outstanding_findings: list[PlanningFinding] = []


class PlanSpec(BaseModel):
    title: str
    scope: str
    approach: str
    components: list[PlanComponent] = []
    risks: list[PlanRisk] = []
    open_questions: list[str] = []
    reviewer_outcome: ReviewerOutcome
    plan_markdown: str
    confirmed_understanding: bool
    generated_at: str


class PlanningSession(BaseModel):
    id: str
    project_id: str
    project_name: str
    sandbox_id: str
    title: str
    status: PlanningStatus
    turn_state: PlanningTurnState
    clarifier_provider: AgentProvider
    planner_provider: AgentProvider
    reviewer_provider: AgentProvider
    max_review_turns: int
    review_turn: int
    plan_revision: int
    confirmed: bool
    understanding_summary: str = ""
    failure_reason: str = ""
    created_at: str
    updated_at: str
    settled_at: str | None = None


class PlanningSessionDetail(PlanningSession):
    feature_brief: str = ""
    messages: list[PlanningMessage] = []
    findings: list[PlanningFinding] = []
    plan_spec: PlanSpec | None = None


class PlanningSessionsResponse(BaseModel):
    count: int
    sessions: list[PlanningSession]
```

`AgentProvider` is imported from `app.agents.models`.

### 2.4 Store methods

Add to `ControllerStore`, placed after the task methods. Follow the existing
style: keyword-only arguments, `_now()` for timestamps, `_json` for JSON
columns, `_row` on reads, and an `_event` write inside the same transaction for
anything that changes a session's phase.

```python
def create_planning_session(self, *, session_id: str, project_id: str,
                            sandbox_id: str, project_name: str, title: str,
                            status: str, clarifier_provider: str,
                            planner_provider: str, reviewer_provider: str,
                            credential_profile: str,
                            max_review_turns: int) -> None: ...

def planning_session(self, session_id: str) -> dict[str, Any] | None: ...

def planning_sessions_for_project(self, project_id: str) -> list[dict[str, Any]]: ...

def advance_planning_status(self, *, session_id: str,
                            from_statuses: Iterable[str], to_status: str,
                            settled: bool = False,
                            failure_reason: str | None = None) -> bool: ...

def claim_planning_turn(self, session_id: str) -> bool: ...

def release_planning_turn(self, session_id: str) -> None: ...

def running_planning_sessions(self) -> list[dict[str, Any]]: ...

def append_planning_message(self, *, session_id: str, role: str, text: str,
                            payload: Mapping[str, Any] | None = None,
                            raw_output: str = "",
                            revision: int | None = None) -> int: ...

def planning_messages(self, session_id: str) -> list[dict[str, Any]]: ...

def set_planning_understanding(self, *, session_id: str, summary: str) -> None: ...

def freeze_planning_brief(self, *, session_id: str, brief: str,
                          confirmed: bool) -> None: ...

def record_plan_revision(self, *, session_id: str, revision: int,
                         plan_json: Mapping[str, Any],
                         plan_markdown: str) -> None: ...

def record_review_result(self, *, session_id: str, revision: int,
                         approved: bool, summary: str) -> None: ...

def plan_revisions(self, session_id: str) -> list[dict[str, Any]]: ...

def upsert_planning_finding(self, *, session_id: str, finding_id: str,
                            severity: str, text: str, status: str,
                            round_number: int) -> None: ...

def set_finding_response(self, *, session_id: str, finding_id: str,
                         status: str, planner_response: str) -> None: ...

def resolve_unseen_findings(self, *, session_id: str, round_number: int) -> int: ...

def planning_findings(self, session_id: str) -> list[dict[str, Any]]: ...

def set_plan_spec(self, *, session_id: str, plan_spec: Mapping[str, Any]) -> None: ...
```

Behaviour that is not obvious from the name:

- **`advance_planning_status`** is a single guarded `UPDATE` copied from
  `advance_task_status` (`store.py:484`): the source statuses go in the `WHERE`
  clause, the method returns whether the row moved, and it writes a
  `planning.status` event with the new status. `settled=True` sets
  `settled_at`. `failure_reason` is written with `COALESCE`, so passing `None`
  leaves it alone.
- **`claim_planning_turn`** is
  `UPDATE planning_sessions SET turn_state = 'running', updated_at = ? WHERE id = ? AND turn_state = 'idle'`
  and returns `cursor.rowcount == 1`. This is the only concurrency guard for
  turns. Do not add a Python-side check.
- **`append_planning_message`** computes the next `sequence` as
  `SELECT COALESCE(MAX(sequence), 0) + 1` inside the same transaction and
  returns it. The `UNIQUE (session_id, sequence)` index is what makes a race
  fail loudly.
- **`resolve_unseen_findings`** sets `status = 'resolved'` for every finding of
  the session whose `status` is not already `resolved` and whose
  `last_seen_round` is below `round_number`. Returns the count. This is the rule
  that makes "the reviewer did not re-raise it" mean resolved.
- **`upsert_planning_finding`** inserts, or on conflict updates `severity`,
  `text`, `status`, `last_seen_round`, and `updated_at`, leaving
  `raised_in_round` at its original value.
- **`running_planning_sessions`** returns every session whose `turn_state` is
  `running`. Only the restart reconciliation uses it.

### 2.5 Tests

`backend/tests/planning/test_models.py`:

- Every status in `PlanningStatus` appears as a key in `PLANNING_TRANSITIONS`.
- Every non-terminal status has at least one target.
- Every terminal status has none.
- `CANCELLED` is a target of every non-terminal status.
- `source_statuses` agrees with the table for each target.

`backend/tests/planning/test_store.py`, using a `ControllerStore` on `tmp_path`
as `backend/tests/controller/test_store.py` already does:

- `initialize` on a fresh database creates the four tables and records version 7.
- `initialize` twice is a no-op.
- Creating a session for an unregistered sandbox raises `sqlite3.IntegrityError`.
- `advance_planning_status` from a wrong source status returns `False` and
  leaves the row unchanged.
- `claim_planning_turn` returns `True` once, then `False`, then `True` again
  after `release_planning_turn`.
- `append_planning_message` numbers sequences from 1 upward.
- `resolve_unseen_findings` resolves only findings below the given round.
- `set_plan_spec` round-trips a nested document.

---

## 3. Phase 1 — Model runner and prompt contracts

**Owned files**

- `backend/app/planning/config.py` (new)
- `backend/app/planning/prompts.py` (new)
- `backend/app/planning/runner.py` (new)
- `backend/app/agents/service.py` (edit: one public alias only, see 3.2)
- `backend/tests/planning/test_runner.py` (new)
- `backend/tests/planning/test_prompts.py` (new)

The `agents/service.py` edit is a single added line exposing the existing
credential-volume helper under a public name. Change nothing else in that file:
`create_agent`, its container flags, and the helper's body all stay as they are.

This phase must not import `app.planning.service` or the store. It takes a
prompt and returns a parsed payload.

### 3.1 Settings

`backend/app/planning/config.py`, in the shape of
`backend/app/previews/config.py`: a frozen dataclass plus an `lru_cache`
factory.

```python
@dataclass(frozen=True)
class PlanningSettings:
    clarifier_provider: AgentProvider
    planner_provider: AgentProvider
    reviewer_provider: AgentProvider
    credential_profile: str
    max_review_turns: int
    turn_timeout_seconds: int
    planning_memory: str
    claude_model: str
    codex_model: str
    codex_reasoning_effort: str
```

Environment variables and defaults:

| Variable | Default |
|---|---|
| `PLANNING_CLARIFIER_PROVIDER` | `claude` |
| `PLANNING_PLANNER_PROVIDER` | `claude` |
| `PLANNING_REVIEWER_PROVIDER` | `codex` |
| `PLANNING_CREDENTIAL_PROFILE` | `default` |
| `PLANNING_MAX_REVIEW_TURNS` | `3` |
| `PLANNING_TURN_TIMEOUT_SECONDS` | `600` |
| `PLANNING_MEMORY` | `2g` |
| `PLANNING_CLAUDE_MODEL` | `opus` |
| `PLANNING_CODEX_MODEL` | `gpt-5.6-terra` |
| `PLANNING_CODEX_REASONING_EFFORT` | `high` |

An unrecognised provider value falls back to the default rather than raising at
import time.

### 3.2 Invocation

`backend/app/planning/runner.py`.

```python
PLANNING_WORKSPACE = "/workspace"
PLANNING_CREDENTIALS = "/auth"
PROMPT_VARIABLE = "PLANNING_PROMPT"


@dataclass(frozen=True)
class TurnRequest:
    role: PlanningRole
    provider: AgentProvider
    prompt: str
    project_volume: str


@dataclass(frozen=True)
class TurnResult:
    raw_output: str
    payload: dict[str, Any]


class PlanningTurnError(Exception):
    def __init__(self, status_code: int, detail: str, raw_output: str = "") -> None: ...
```

The prompt travels in an environment variable, never in `argv`. This keeps the
container read-only, avoids argv length limits, and keeps the prompt out of
`docker inspect`'s command field.

**Claude command**

```python
[
    "sh", "-c",
    'exec claude -p "$PLANNING_PROMPT"'
    ' --output-format json'
    f' --model {shlex.quote(settings.claude_model)}'
    ' --permission-mode plan'
    ' --allowedTools "Read,Glob,Grep"'
    ' < /dev/null',
]
```

`--permission-mode plan` is Claude Code's read-only planning mode and
`--allowedTools` narrows it further. Stdout is a JSON envelope; the model's text
is the `result` field. Unwrap it, then extract the payload from that string.

**Codex command**

```python
[
    "sh", "-c",
    'codex exec "$PLANNING_PROMPT"'
    ' --sandbox read-only'
    ' --ephemeral'
    ' --skip-git-repo-check'
    ' -C /workspace'
    f' -m {shlex.quote(settings.codex_model)}'
    f' -c model_reasoning_effort={shlex.quote(settings.codex_reasoning_effort)}'
    ' --output-last-message /tmp/planning-output.json'
    ' < /dev/null'
    ' && cat /tmp/planning-output.json',
]
```

`< /dev/null` is mandatory. Without it `codex exec` waits on stdin forever and
the turn dies at the timeout with zero CPU used. `--output-last-message` puts
the final message in a file so the JSONL event stream never has to be parsed.

**Container**

Create, start, wait, read logs, remove. `containers.run(detach=False)` has no
timeout, so it cannot be used.

```python
container = docker_client.containers.create(
    image=provider.image,
    command=command,
    auto_remove=False,
    init=True,
    read_only=True,
    cap_drop=["ALL"],
    security_opt=["no-new-privileges:true"],
    pids_limit=512,
    mem_limit=settings.planning_memory,
    working_dir=PLANNING_WORKSPACE,
    environment={
        provider.credential_environment_variable: PLANNING_CREDENTIALS,
        "HOME": "/tmp/home",
        "TERM": "dumb",
        PROMPT_VARIABLE: request.prompt,
    },
    labels=labels,
    volumes={
        request.project_volume: {"bind": PLANNING_WORKSPACE, "mode": "ro"},
        credential_volume.name: {"bind": PLANNING_CREDENTIALS, "mode": "rw"},
    },
    tmpfs={"/tmp": "rw,nosuid,size=256m"},
)
```

Differences from `agents/service.py:156`, and why:

- `"mode": "ro"` on the project volume. This is the authority boundary. A
  planning turn can read the code and can never change it.
- No dependency volume. Planning does not build.
- No `network_disabled`. The model provider is reachable over the network.
- `auto_remove=False`, because the logs must be read after exit. Remove the
  container in a `finally` block.

Labels: reuse `LABEL_CONTROLLER_MANAGED` and `LABEL_KIND` from
`app.previews.service` with `kind="planning"`, plus the session id and the role,
so reconciliation and cleanup can find these containers.

Get the credential volume by calling `_credential_volume` from
`app.agents.service`. Export it under a public name in that module rather than
duplicating the get-or-create logic; a second implementation would eventually
create a second volume and split the login.

**Timeout and exit**

```python
exit_status = container.wait(timeout=settings.turn_timeout_seconds)
```

`requests.exceptions.ReadTimeout` from `wait` means the turn timed out: kill the
container and raise `PlanningTurnError(504, f"{role} turn timed out after …")`.
A non-zero `StatusCode` raises `PlanningTurnError(502, …)` carrying the last
2000 characters of the container logs.

### 3.3 Payload extraction

One function, used by every role:

```python
def extract_payload(raw: str, *, provider: AgentProvider) -> dict[str, Any]:
```

1. For Claude, parse stdout as JSON and take `result`; if that fails, fall back
   to treating stdout as the model text.
2. Scan the text for the first `{` and walk forward counting braces outside
   string literals until the object closes. Parse that slice.
3. On failure raise `PlanningTurnError(422, …, raw_output=raw)`.

Brace counting must ignore braces inside JSON strings and must honour
backslash escapes. A plan's markdown routinely contains braces.

### 3.4 Repair turn

```python
def run_turn_with_repair(docker_client, settings, request, validate) -> TurnResult:
```

Runs the turn. Calls `validate(payload)`, which raises `ValueError` on a schema
breach. On `PlanningTurnError(422)` or `ValueError`, runs exactly one more turn
whose prompt is the original prompt plus:

```
Your previous reply could not be used. Error: <message>

Previous reply:
<the raw output, truncated to 4000 characters>

Reply again with one JSON object and nothing else.
```

A second failure raises. Never loop more than twice.

### 3.5 Prompts

`backend/app/planning/prompts.py`. Pure functions, no I/O, so they are cheap to
test.

```python
def clarifier_prompt(*, title: str, messages: Sequence[Mapping[str, Any]]) -> str: ...
def planner_prompt(*, brief: str, round_number: int,
                   previous_turns: Sequence[Mapping[str, Any]],
                   ledger: Sequence[Mapping[str, Any]]) -> str: ...
def reviewer_prompt(*, brief: str, plan_markdown: str,
                    ledger: Sequence[Mapping[str, Any]]) -> str: ...
def feature_brief(*, title: str, request: str, understanding: str,
                  messages: Sequence[Mapping[str, Any]], confirmed: bool) -> str: ...
```

Every prompt ends with the JSON schema from design spec section 4 and the line
`Reply with one JSON object and nothing else.`

Content requirements, taken from the design spec and not to be softened:

- **Clarifier.** State that it must not produce a plan, an implementation, or a
  design. State that it asks at most three questions per reply, chosen because
  the previous answer made them the next most useful. List the areas to cover
  across the conversation: desired outcome, scope in and out, constraints,
  expected behaviour including errors and edges, and the trade-offs that matter.
  State that it may read the project at `/workspace` read-only to ground its
  questions. State that when it believes the feature is understood it sets
  `ready_to_summarize` to `true`, asks nothing further, and puts its full
  understanding in `understanding_summary`.
- **Planner.** Supply the brief. Supply its own previous turns for round 2 and
  later, oldest first. Supply the ledger. Require a response to every ledger
  finding. State that it plans only: no code, no file writes, no tech spec, no
  task breakdown.
- **Reviewer.** Supply the brief, the current plan, and the ledger. State
  plainly that prior findings are context, not truth; that it must assess the
  current plan from scratch; that it must not reopen an `answered` finding
  without a concrete reason drawn from the current plan; that it must either
  accept a `rejected` finding's rationale or say precisely why it does not hold;
  and that it must name every remaining and every newly introduced issue. State
  the id rule: reuse a ledger id when re-raising, use `NEW-1`, `NEW-2`, … for
  new findings.

### 3.6 Tests

`backend/tests/planning/test_prompts.py`:

- The clarifier prompt contains every earlier message in order.
- The planner prompt for round 1 contains no ledger section; for round 2 it
  contains the previous plan and every ledger finding.
- The reviewer prompt contains the ledger but not the planner's transcript.
- Every prompt ends with the JSON instruction line.

`backend/tests/planning/test_runner.py`, with a stub Docker client in the shape
of `backend/tests/tasks/test_service.py:34`:

- The Claude command contains `--permission-mode plan`, `--allowedTools`, and
  `--output-format json`.
- The Codex command contains `--sandbox read-only` and ends its `codex exec`
  invocation with `< /dev/null`.
- The project volume is mounted `ro` and the credential volume `rw`.
- `network_disabled` is not set.
- The prompt is passed in the environment, not in the command.
- A payload wrapped in prose is extracted.
- A payload whose markdown contains braces and brace-bearing strings is
  extracted whole.
- Malformed output triggers exactly one repair turn; a second failure raises.
- A non-zero exit raises `PlanningTurnError` with the log tail.
- A `ReadTimeout` from `wait` kills the container and raises with 504.
- The container is removed even when the turn raises.

---

## 4. Phase 2 — Clarify phase: service, router, background turns

**Owned files**

- `backend/app/planning/service.py` (new)
- `backend/app/planning/router.py` (new)
- `backend/app/main.py` (edit: import and `include_router`)
- `backend/app/projects/service.py` (edit: add `ensure_sandbox_registered`)
- `backend/app/agents/service.py` (edit: call it from `create_agent`)
- `backend/app/controller/lifecycle.py` (edit: reconcile running turns)
- `backend/tests/planning/test_service.py` (new)
- `backend/tests/planning/test_router.py` (new)
- `backend/tests/planning/test_reconcile.py` (new)

### 4.1 Router

```python
router = APIRouter(prefix="/projects/{project_name}/planning", tags=["planning"])
```

Copy `_docker_response` from `backend/app/tasks/router.py:29` verbatim, renaming
the operation error to `PlanningOperationError`. Add
`except PlanningTurnError` mapping to its own `status_code`.

| Method | Path | Request | Response | Status |
|---|---|---|---|---|
| POST | `/sessions` | `CreatePlanningSessionRequest` | `PlanningSession` | 201 |
| GET | `/sessions` | — | `PlanningSessionsResponse` | 200 |
| GET | `/sessions/{session_id}` | — | `PlanningSessionDetail` | 200 |
| POST | `/sessions/{session_id}/messages` | `PlanningMessageRequest` | `PlanningSession` | 202 |
| POST | `/sessions/{session_id}/confirm` | — | `PlanningSession` | 202 |
| POST | `/sessions/{session_id}/correct` | `PlanningMessageRequest` | `PlanningSession` | 202 |
| POST | `/sessions/{session_id}/proceed` | — | `PlanningSession` | 202 |
| POST | `/sessions/{session_id}/cancel` | — | `PlanningSession` | 200 |

Every endpoint validates that the session belongs to the named project and
returns 404 otherwise. A session id from another project must not be reachable
through this prefix.

Register in `backend/app/main.py` beside the other routers, keeping the existing
alphabetical import order: `from app.planning.router import router as planning_router`,
then `app.include_router(planning_router)`.

### 4.2 Sandbox registration

`planning_sessions.sandbox_id` has a foreign key, and a project that has never
had a coding agent has no `sandboxes` row, because `register_sandbox` is called
from `create_agent` (`agents/service.py:92`).

Add to `backend/app/projects/service.py`:

```python
def ensure_sandbox_registered(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    project_name: str,
) -> tuple[str, str, ProjectRegistration]:
    """Returns (sandbox_id, project_id, project), registering the sandbox row."""
```

Move the derivation currently inline at `agents/service.py:87-100` into this
function without changing it: the `sandbox_id` fallback hash over
`f"sandbox:{project.volume_name}"` truncated to 32 characters, the
`f"legacy:{project.name}"` source-path fallback, and the `status="ready"`
argument. Then make `create_agent` call it. **The derivation must stay
byte-identical**; a different `sandbox_id` for the same volume creates a second
sandbox row and splits a project's history.

The planning service calls it before creating a session, and rejects a project
that is not ready with 409, matching `create_agent`.

### 4.3 Service

```python
class PlanningOperationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None: ...


def create_session(docker_client, controller_store, settings, project_name,
                   request) -> PlanningSession: ...
def list_sessions(docker_client, controller_store, project_name) -> PlanningSessionsResponse: ...
def get_session(controller_store, project_name, session_id) -> PlanningSessionDetail: ...
def post_message(controller_store, settings, project_name, session_id, request) -> PlanningSession: ...
def confirm_understanding(controller_store, settings, project_name, session_id) -> PlanningSession: ...
def correct_understanding(controller_store, settings, project_name, session_id, request) -> PlanningSession: ...
def proceed_without_confirmation(controller_store, settings, project_name, session_id) -> PlanningSession: ...
def cancel_session(controller_store, project_name, session_id) -> PlanningSession: ...
```

**`create_session`**

1. `ensure_sandbox_registered`. Reject a project that is not ready with 409.
2. Resolve providers: request override, else settings default. Resolve
   `max_review_turns` the same way.
3. `create_planning_session` with `status=clarifying`, a `uuid4().hex` id.
4. `append_planning_message` with role `user` and the request text.
5. Schedule a clarifier turn.

**`post_message`**

1. Load the session; 404 on a project mismatch.
2. Reject a terminal status with 409.
3. Reject `turn_state == running` with 409, message
   `"A planning turn is already running for this session"`.
4. Append the message with role `user`.
5. Schedule a clarifier turn.

Sessions in `awaiting_confirmation` reject plain messages with 409 and a message
naming the three available actions. `correct_understanding` is the way to reply
in that state; it appends the correction, moves the session back to
`clarifying`, and schedules a clarifier turn.

**`confirm_understanding`** requires `awaiting_confirmation`. It builds the
feature brief with `prompts.feature_brief(..., confirmed=True)`,
`freeze_planning_brief`, advances to `planning`, and schedules a planner turn.

**`proceed_without_confirmation`** accepts `clarifying` or
`awaiting_confirmation`, rejects a running turn with 409, builds the brief with
`confirmed=False`, and does the same.

**`cancel_session`** advances to `cancelled` from any non-terminal status with
`settled=True`. It does not touch a running container. The background worker
discards its result.

### 4.4 Background turns

FastAPI's event loop runs the router; the store and the Docker SDK are both
synchronous. Use `asyncio.to_thread`, as `main.py`'s `lifespan` already does for
`reconcile_controller_state`.

```python
def schedule_turn(session_id: str, kind: TurnKind) -> None:
    asyncio.get_running_loop().create_task(_run_turn(session_id, kind))
```

Hold a module-level `set[asyncio.Task]` and discard each task in its done
callback, so a task is never garbage-collected mid-flight.

`_run_turn` in outline:

1. `claim_planning_turn`. If it returns `False`, return: another turn owns the
   session.
2. Run the model in a worker thread via `asyncio.to_thread`.
3. Reload the session. **If its status is terminal, append the raw output as a
   `system` message for audit, release the turn, and stop.** This is what makes
   cancel safe.
4. Apply the result.
5. `release_planning_turn` in a `finally` block, always.

A `PlanningTurnError` advances the session to `failed` with
`failure_reason` set, and appends the raw output as a `system` message.

Clarifier result handling:

- Append a `clarifier` message. Store `questions` in `payload_json`.
- `ready_to_summarize` true: `set_planning_understanding`, then advance
  `clarifying → awaiting_confirmation`.
- Otherwise the session stays `clarifying` and waits for the human.

### 4.5 Restart reconciliation

In `reconcile_controller_state` (`controller/lifecycle.py:27`): for every session
from `running_planning_sessions()`, advance it to `failed` with
`failure_reason="The backend restarted while this turn was running"` and release
the turn. Add a `planning` key to the returned counts dictionary.

**Placement matters.** That function wraps its whole body in
`try: … except DockerException: return counts`, because everything it currently
does needs a Docker client. Planning reconciliation needs only the store, so put
it **before** the `try`, not inside it. Inside, a Docker daemon that is down
would skip it, and every session caught mid-turn by the restart would stay
`running` forever with no path back: nothing else releases a turn, and every
later action on that session returns 409.

Write a test that proves this: reconcile with a Docker client that raises
`DockerException`, and assert the stranded session still reaches `failed`.

A session must never be left `running` after a restart. Nothing would ever
release it, and every later action would return 409.

### 4.6 Tests

`backend/tests/planning/test_service.py`, stubbing the runner so no container
starts:

- Creating a session stores the request as sequence 1 and sets `clarifying`.
- Creating a session for a project that is not ready returns 409.
- A second message while a turn runs returns 409.
- A message on a terminal session returns 409.
- A plain message in `awaiting_confirmation` returns 409; `correct` succeeds and
  returns the session to `clarifying`.
- `ready_to_summarize` moves the session to `awaiting_confirmation` and stores
  the summary.
- `confirm` freezes a brief containing the title, the original request, the
  understanding, and the question-and-answer pairs, and moves to `planning`.
- `proceed` from `clarifying` freezes a brief with `confirmed=False`.
- `cancel` during a running turn settles the session, and the turn's result is
  discarded rather than applied.
- A `PlanningTurnError` fails the session and records the reason.
- The turn is released on every path, including the raising one.

`backend/tests/planning/test_router.py`, in the shape of
`backend/tests/tasks/test_router.py`:

- Each endpoint's status code and response model.
- A session id from another project returns 404.

`backend/tests/planning/test_reconcile.py`:

- A session left `running` becomes `failed` with `turn_state` back to `idle`.

---

## 5. Phase 3 — Plan and review loop, ledger, Plan Spec

**Owned files**

- `backend/app/planning/service.py` (edit)
- `backend/app/planning/prompts.py` (edit, if the ledger rendering needs it)
- `backend/tests/planning/test_review_loop.py` (new)

Do not change the router. The loop is entirely internal; the human only polls.

### 5.1 Planner turn

1. Build the prompt from the frozen brief, the planner's own previous turns
   oldest first, and the ledger.
2. Run it. Validate against design spec section 4.2.
3. `revision = session["plan_revision"] + 1`. `record_plan_revision`.
4. Append a `planner` message carrying `revision`.
5. Apply `finding_responses`: for each entry whose `finding_id` is in the
   ledger, `set_finding_response` with `answered` or `rejected` and the
   rationale. Discard unknown ids. A ledger finding with no response stays
   `open`.
6. Advance `planning → under_review`. Schedule the reviewer turn.

The planner's previous turns come from `planning_messages` filtered to role
`planner`. That is what "persistent planner session" means here: replay, not
`--resume`.

### 5.2 Reviewer turn

1. Build the ledger: findings whose status is `open`, `answered`, or `rejected`,
   in the shape from design spec section 4.4. `resolved` findings are omitted.
2. Build the prompt from the brief, the current revision's markdown, and that
   ledger. **Do not include planner messages.**
3. Run it. Validate against design spec section 4.3.
4. `round_number = session["review_turn"] + 1`.
5. Normalise ids. For each returned finding: an id already in the ledger is
   reused; an id matching `NEW-*` or otherwise unknown is minted as
   `F{n}` where `n` is one past the highest existing numeric suffix for that
   session. Never persist a model-supplied id that is not already known.
6. `upsert_planning_finding` for each, with `status='open'` and
   `last_seen_round=round_number`.
7. `resolve_unseen_findings(round_number=round_number)`.
8. `record_review_result` on the revision.
9. Append a `reviewer` message carrying `revision`.

Then decide:

- `approved` is true **and** no persisted finding for this round has severity
  `blocking` or `major`: write the Plan Spec, advance
  `under_review → plan_ready`, settled.
- `approved` is true but a blocking or major finding was raised: treat it as a
  rejection. Append a `system` message stating that the verdict was overridden
  because a blocking or major finding was raised, then follow the rejection
  path.
- Rejected and `round_number < max_review_turns`: advance
  `under_review → planning` and schedule another planner turn.
- Rejected and `round_number >= max_review_turns`: write the Plan Spec, advance
  `under_review → review_limit_reached`, settled.

Increment `review_turn` in the same transaction that records the review result,
so a crash cannot double-count a round.

### 5.3 Plan Spec

Assembled by the controller from stored rows, never by asking a model for it.

```python
def build_plan_spec(session: Mapping[str, Any],
                    revision: Mapping[str, Any],
                    findings: Sequence[Mapping[str, Any]],
                    approved: bool) -> PlanSpec:
```

- `title`, `scope`, `approach`, `components`, `risks`, `open_questions`, and
  `plan_markdown` come from the latest revision's `plan_json`.
- `reviewer_outcome.approved` is the loop's verdict, not the model's claim.
- `reviewer_outcome.rounds` is `session["review_turn"]`.
- `reviewer_outcome.outstanding_findings` holds every finding not `resolved`.
  It is empty on an approved spec and populated at the review limit.
- `confirmed_understanding` is `session["confirmed"]`. When it is false, prepend
  one sentence to `scope` recording that the human chose to proceed without
  confirming a summary.
- `generated_at` is `_now()`.

Write it with `set_plan_spec` in the same call that settles the session.

### 5.4 Tests

`backend/tests/planning/test_review_loop.py`, with a scripted stub runner that
returns a queued sequence of payloads:

- Approval on round 1 gives `plan_ready`, one revision, and an empty
  `outstanding_findings`.
- Rejection then approval gives `plan_ready` and `rounds == 2`.
- Three rejections with `max_review_turns=3` give `review_limit_reached`, a
  Plan Spec, `approved` false, and the outstanding findings listed.
- The round-2 planner prompt contains the round-1 plan and every ledger finding.
- The round-2 reviewer prompt contains the ledger and no planner message.
- A finding re-raised in round 2 keeps its round-1 id and its
  `raised_in_round`.
- A finding not re-raised becomes `resolved` and leaves the ledger.
- A `NEW-1` id is minted to a stable `F{n}` and re-raised under that id in the
  next round.
- `approved: true` alongside a `blocking` finding is treated as a rejection and
  records a `system` message.
- A planner turn that omits a response leaves that finding `open`.
- No test path writes to the project volume, and every container mount for the
  workspace is `ro`.

---

## 6. Phase 4 — Frontend API client and project section

**Owned files**

- `frontend/src/api/planning.ts` (new)
- `frontend/src/components/ProjectPlanningSection.tsx` (new)
- `frontend/src/components/PlanningStatusBadge.tsx` (new)
- `frontend/src/pages/ProjectDetailPage.tsx` (edit: render the section)

### 6.1 API client

`frontend/src/api/planning.ts`, using only `getJson` and `postJson` from
`./client`. Mirror `frontend/src/api/agents.ts`: exported interfaces matching
the backend models one field at a time, then one function per endpoint.

```ts
export type PlanningStatus =
  | 'clarifying' | 'awaiting_confirmation' | 'planning' | 'under_review'
  | 'plan_ready' | 'review_limit_reached' | 'failed' | 'cancelled'

export function fetchPlanningSessions(projectName: string, signal?: AbortSignal): Promise<PlanningSessionsResponse>
export function fetchPlanningSession(projectName: string, sessionId: string, signal?: AbortSignal): Promise<PlanningSessionDetail>
export function createPlanningSession(projectName: string, body: CreatePlanningSessionBody): Promise<PlanningSession>
export function sendPlanningMessage(projectName: string, sessionId: string, text: string): Promise<PlanningSession>
export function confirmPlanningUnderstanding(projectName: string, sessionId: string): Promise<PlanningSession>
export function correctPlanningUnderstanding(projectName: string, sessionId: string, text: string): Promise<PlanningSession>
export function proceedPlanningSession(projectName: string, sessionId: string): Promise<PlanningSession>
export function cancelPlanningSession(projectName: string, sessionId: string): Promise<PlanningSession>
```

Every path segment goes through `encodeURIComponent`, as `api/agents.ts` does.

Add `PLANNING_TERMINAL_STATUSES` and a `isPlanningTerminal(status)` helper here,
so the section and the page share one definition of "stop polling".

### 6.2 Status badge

`PlanningStatusBadge.tsx`, in the shape of
`frontend/src/components/CopyStatusBadge.tsx`. Labels are the human-readable
forms: `Clarifying`, `Awaiting your confirmation`, `Planning`, `Under review`,
`Plan ready`, `Review limit reached`, `Failed`, `Cancelled`, mapped to the
`pill` classes in design spec section 5.1.

`CopyStatusBadge` carries a comment stating its rule: *status carries an icon
and a word, never colour alone*. Follow it. Supply an icon per status alongside
the label, as that component does.

### 6.3 Section

`ProjectPlanningSection.tsx`, taking `projectName` and `projectReady`, built
from `ProjectAgentsSection.tsx`:

- `useApiResource` over `fetchPlanningSessions`.
- A **Plan a feature** primary button, disabled when `projectReady` is false,
  with the reason shown.
- A form with title, a textarea for the request, three provider selects
  defaulting to blank meaning "use the default", and a review-limit number
  input. Submit calls `createPlanningSession`, then navigates to
  `/projects/<name>/plans/<id>`.
- A session list, newest first, each row linking to the session page and showing
  title, `PlanningStatusBadge`, providers, round when relevant, and
  `formatRelativeTime(created_at)` with `formatTimestamp` in the `title`
  attribute, as the other sections do.
- Poll every 3 seconds while any listed session is non-terminal, in the shape of
  the copy-job poll at `ProjectDetailPage.tsx:57`.

Render it in `ProjectDetailPage.tsx` between `ProjectAgentsSection` and
`ProjectPreviewSection`, passing `projectName={data.name}` and
`projectReady={data.ready}`.

Reuse existing classes: `card`, `card-header`, `card-body`, `section-heading`,
`button-row`, `pill`, `status`, `mono`. Add no new CSS unless a layout genuinely
has no existing equivalent; if it does, add it to `frontend/src/App.css` beside
the related section rules.

---

## 7. Phase 5 — Frontend session page and route

**Owned files**

- `frontend/src/pages/PlanningSessionPage.tsx` (new)
- `frontend/src/components/PlanSpecView.tsx` (new)
- `frontend/src/App.tsx` (edit: one route)

### 7.1 Route

Every page in `App.tsx` is code-split, so registration is two additions, not
one. First the lazy import, beside the others at the top of the file:

```tsx
const PlanningSessionPage = lazy(() => import('./pages/PlanningSessionPage'))
```

Then the route, beside the agent route at line 221:

```tsx
<Route
  path="/projects/:projectName/plans/:sessionId"
  element={<PlanningSessionPage />}
/>
```

The existing `<Suspense>` boundary already covers it. Do not add another.
`App.tsx` also carries uncommitted theme work (`useTheme`, `ThemeChoice`,
`THEME_OPTIONS`); leave all of it alone.

### 7.2 Page

`useParams` for both ids, `useApiResource` over `fetchPlanningSession`. Poll
every 2 seconds while the session is non-terminal, using `isPlanningTerminal`
from the API module. Stop when it settles.

Regions in the order given by design spec section 5.2. Details that the design
spec leaves to implementation:

- The breadcrumb copies `ProjectDetailPage.tsx:71-79`: `Projects` / project name
  / session title.
- The thinking marker renders only when `turn_state === 'running'` and names the
  role implied by the status: clarifier for `clarifying` and
  `awaiting_confirmation`, planner for `planning`, reviewer for `under_review`.
- The composer is disabled while `turn_state === 'running'`, with the reason in
  a `status` paragraph rather than a tooltip.
- **Proceed anyway** sits beside Send, is enabled in `clarifying` and
  `awaiting_confirmation` when the turn is idle, and asks for confirmation
  through `ConfirmDialog` with a plain sentence: the planner will work from what
  has been said so far.
- **Cancel session** uses `ConfirmDialog` without a confirm phrase; cancelling a
  plan destroys nothing.
- Clarifier questions render as an ordered list from the message's `questions`
  field, above the message text if the model put a preamble there.
- Planner and reviewer messages carry a `Revision n` or `Review round n` label.

### 7.3 Plan Spec view

`PlanSpecView.tsx` takes a `PlanSpec` and renders:

1. A summary block: scope, approach, component names, the highest-severity
   risks, and the reviewer outcome as a sentence naming the round count.
2. When `reviewer_outcome.approved` is false, a `status status-error` paragraph
   stating that the reviewer did not approve this plan, followed by the
   outstanding findings as a list with their severities.
3. The full `plan_markdown` inside a `<details>` element.

Markdown is rendered as pre-formatted text. Do not add a markdown library; the
project has no frontend dependency for it and this phase does not add one.

---

## 8. Rules for every phase

- **No repository writes.** No phase may mount the project volume read-write, run
  a git command, create a branch, create a task, or start a coding agent. If a
  phase seems to need one, the phase is wrong; stop and report.
- **No tree-wide git commands.** No `git add -A`, `git checkout .`, `git stash`,
  or `git reset --hard`. Stage only files you own.
- **Report, do not fix, failures outside your owned files.**
- **Do not change `AgentProvider`, the agent images, or `create_agent`'s
  container flags.** Phase 2's only edit to `agents/service.py` is the extraction
  described in section 4.2.
- **Do not add a Python dependency.** Every phase is buildable with the current
  `backend/pyproject.toml`. If a phase appears to need an HTTP client for a model
  provider, it has misread the ADR: models run as containers.
- **Do not add a frontend dependency.**
- Run `cd backend && uv run pytest` before reporting a backend phase complete,
  and `cd frontend && npm run build` before reporting a frontend phase complete.
  Quote the real output. Do not estimate a test count.
