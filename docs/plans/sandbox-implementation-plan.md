# Implementation plan: managed v1 feature sandboxes

**Deliverable:** execution plan derived from `docs/plans/sandbox-architecture-proposal.md`
plus the six accepted architecture decisions. No ADR, no production code.

**Verified against:** the working tree at `34b6040` (the same commit the proposal
reviewed). Every anchor below was re-checked by reading the current code. Where the
proposal and the code disagree, the code is quoted and the difference is stated.

---

## Context

Sandboxes today are `tar` copies of a host folder with a random ID, a name-keyed
identity, no base commit, no sync, no remote, and a database whose lifetime belongs to
a preview run. The target is an independent per-feature environment: deterministic
identity, an independent clone pinned to a known `origin/main` commit, one database per
sandbox, explicit sync, and controller-owned publishing.

The proposal argues that architecture. This document does not re-argue it. It converts
proposal section 6 into ordered, executable phases and records where the accepted
decisions change the proposal.

---

# 1. Implementation readiness

## 1.1 Accepted architecture decisions

These are settled inputs, not open questions.

| # | Decision | Effect on this plan |
|---|---|---|
| 1 | New managed v1 sandboxes require a Git remote with `origin/main`. Local-only folders stay legacy. | Phase 4 requires a remote. Phase 2 keeps the legacy create path intact. |
| 2 | The human supplies an immutable `feature_key` at create. Planning happens **after** sandbox creation, inside that sandbox. | Phase 2 adds `feature_key` to the create request. Planning is unchanged — it already attaches to an existing sandbox. |
| 3 | One persistent, replaceable **main-agent environment** per sandbox; delegated work runs in short-lived workers. Container existence is not writer activity. | Phase 5 splits "agent exists" from "agent is writing". This is the largest change from the proposal. |
| 4 | Legacy sandboxes keep local delivery. v1 publishes via remote branch + PR. | Phase 8 adds a second publish path; `delegation/delivery.py` is untouched for legacy. |
| 5 | Database engine is detected per project, proposed, and human-confirmed when uncertain. | Phase 6. Engine is never switched silently on sync. |
| 6 | Migration and seed commands are **project-owned**. The controller discovers and orchestrates; the restricted sandbox runtime executes. | Phase 6, and it is *closer to today's code* than the proposal assumed. See 1.2 Adjustment B. |

## 1.2 Proposal adjustments caused by those decisions

### Adjustment A — agent model

The proposal's "one deterministic agent container per active sandbox" (11.3) and its
writer table (5.4.2) treat any active `agent_runs` row as a writer. Under Decision 3
that is wrong: the main-agent container is a persistent idle environment and must not
block `sync`.

The refined model:

```
sandbox
├── persistent main-agent environment   (conversation, inspection, planning, review)
└── disposable delegated workers        (one hardened container per turn, removed after)
```

Concretely, from the code:

* The main-agent container runs `IDLE_COMMAND` — a `sleep` loop (`agents/service.py:54-61`).
  It exists to host a tmux session (`agents/service.py:312-349`); existing means nothing
  about whether anything is being written.
* `one_active_agent_per_sandbox` (`store.py:51-53`) covers `created|running|replacing|stopping`.
  Today that index marks an *idle sleeper* as active. Under the proposal's rule, an idle
  main agent would permanently block `sync`. Decision 3 forbids that.
* **There is currently no representation of "an agent turn is executing."** Verified: the
  provider CLI is started by `exec` into the idle container, and nothing records it.
  The only per-turn signal is a `task.turn_started` *event* (`tasks/service.py:181-186`),
  which is an append-only audit row, not queryable state.

So Phase 5 must **add** a main-agent writer session. That is new work the proposal did not
scope, and it is the mechanism that makes Decision 3 implementable.

Delegated workers already behave as Decision 3 requires: `run_coding_turn`
(`tasks/runner.py:102`) creates one container per turn and force-removes it in `finally`
(`:167-168`). There is no worker pool and nothing long-lived. Phase 5 preserves and tests
this rather than building it.

### Adjustment B — migration and seed command ownership

The proposal (5.4.3, 7) says migration commands must become **controller-owned
configuration**, supplied or approved by a human at `confirm-engine`, and never read from
agent-editable files.

Repository inspection shows the intended boundary largely exists already, in a better
shape than the proposal assumed:

| Stage | Where it happens today | Matches Decision 6? |
|---|---|---|
| Project defines commands | `package.json` scripts, Prisma schema, Makefile, CI | yes |
| Controller discovers | `previews/detection.py:506-536` (Prisma-only) and `implementation_context/inventory.py` — whose module docstring is literally *"Read the commands that a project defines without running project code"* | yes |
| Human approves | `propose_preview` → `approve_review` (`previews/service.py:403-411`) | yes |
| Restricted runtime executes | `_run_initialization` (`previews/service.py:2614-2677`) — throwaway container, `read_only=True`, `cap_drop=ALL`, `no-new-privileges`, on the run's network which is `internal=True` for isolated access (`:3457`), env limited to `DATABASE_URL` + project secrets | yes |
| Controller executes project commands | **never happens** | correct |

There is also an existing precedent for running project-owned commands with *no* network
at all: `delegation/verification.py:80-106`, whose own comment says *"This container is
the only one that runs the project's own commands"*, using `network_mode="none"`.

**Therefore the plan does not build a controller-owned command registry.** It generalizes
what exists. The chosen minimal design for the detection record:

* **Keep** `migrate_commands_json` / `seed_commands_json`, but redefine them as a
  **resolved command snapshot**: what discovery proposed and a human approved, frozen at
  confirmation time, with the source of each command recorded (`prisma`, `package.json`,
  `manual`).
* **Reason for keeping them:** retryability (`reset-db` must replay exactly what was
  approved, months later, without re-running detection) and auditability (a failed
  migration must be explainable). Re-discovering at every `reset-db` would let an agent
  change what executes by editing a file.
* **Reason they are not controller-owned config:** the values originate from the project,
  not from operator configuration. The controller stores an approved snapshot; it does not
  author it.
* **They are never executed by the controller.** Execution is always a sandbox-runtime
  container. Phase 6 states the exact container.

**One real hazard this uncovers**, which the proposal was right to worry about even though
its diagnosis was off:

* `_manual_configuration` (`detection.py:398-422`) reads `.agent/preview.yaml` **from the
  sandbox workspace volume** — a file a coding agent can write — and it takes precedence
  over detection (`detection.py:198-199`).
* Mitigation exists: `.agent/preview.yaml` is a protected runtime file (`detection.py:88-89`),
  a change forces re-approval (`service.py:202-206`), and hashes are re-checked at start
  (`service.py:359-365`) and after install (`service.py:1183-1198`).
* But `start_preview` does **not** compare `request.config` against the stored proposal —
  it re-digests whatever config the caller submitted (`service.py:392-411`).

**Rule for Phase 6:** sandbox-level migration/seed commands are discovered from
**controller-read project files at a known commit**, never from `.agent/preview.yaml`, and
`reset-db` replays only the stored approved snapshot. Preview-level behavior is unchanged.

### Adjustment C — proposal statements this plan corrects

Differences found by inspection that change effort, ordering, or design.

| # | Proposal says | Code says | Consequence |
|---|---|---|---|
| A1 | "There is no migration runner" (2.3) | True *now* — `initialize` (`store.py:468-475`) runs one `executescript` and stamps version 1. **But** an ordered runner existed at `bd8d06b^`: `MIGRATIONS` mapping, `_apply_migrations`, `_add_column`, `FIRST_RUNNER_MIGRATION = 9`. It was deliberately removed by `bd8d06b` "squash schema migrations". | Phase 1 **restores a known-good pattern** rather than inventing one. Lower risk and effort than the proposal implies. |
| A2 | *(not mentioned)* | `schema_migrations` already contains rows. The live DB is stamped `[1]`. The pre-squash backup `controller.sqlite3.backup-before-empty-reset-20260810T1721+0800` is stamped `[1..17]`, with the same effective schema. | **New migrations must start at version 18.** Numbering from 2 would be silently skipped on any pre-squash database. This is the one genuinely new constraint found. |
| A3 | `register_project` writes the project row | `ControllerStore.register_project` **does not exist**. Project rows are inserted *inside* `register_sandbox` (`store.py:520-527`) with `ON CONFLICT(source_path) DO UPDATE SET id = excluded.id`. `projects/service.py:169 register_project` writes no SQLite at all. | Substance unchanged; Phase 1/2 must target `register_sandbox`, not a non-existent method. |
| A4 | `start_task` runs `ensure_git_baseline` (`:82`) before `create_task` (`:98`), so the row must move | Half true. The row **is** deliberately inserted before the *mutating* git work — `tasks/service.py:114-115`: *"The row is the lock. It exists before git is touched."* Only `ensure_git_baseline` precedes it. | The reorder is narrower than proposed, but still required: `ensure_git_baseline` can `git init` and create a baseline commit on a non-git sandbox, which is a mutation. |
| A5 | `create_agent` records its row after Docker work | Wrong. `start_agent_run` (`agents/service.py:134-138`) precedes `containers.create` (`:147`). It follows `ensure_git_baseline` (`:92`) and dependency-volume resolution (`:124`). | Same narrower reorder as A4. |
| A6 | `delegation/execution.py:163 _execute_run` calls `start_task` unguarded | `claim_run` calls `start_task` at `execution.py:173`; `_execute_run` (`:347`) does not. Admission checks *do* exist for delegation status, work-item state, `one_open_task_per_sandbox`, and `one_running_run_per_delegation`. | The gap is real but specific: no **sandbox-level** lease check. Line anchor corrected. |
| A7 | `create_delegation_revision` at `delegation/service.py:107` | The service function is `create_revision` (`delegation/service.py:88-135`); `create_delegation_revision` is the *store* method it calls (`:113-122`). The row is inserted directly as `ready` (`:119`). | Naming only; the admission point is correct. |
| A8 | `tasks.base_commit` must become nullable for a `preparing` status | `tasks.base_commit` is `NOT NULL` (`store.py:159-176`), so this would force a **second** SQLite table rebuild. But `base_branch` is already inserted as the empty string `""` (`tasks/service.py:103`) and filled in later (`:143-146`). | **Use the existing sentinel convention**: insert `preparing` rows with `base_commit = ""`. No `tasks` rebuild. This is a real simplification over the proposal. |
| A9 | Agent containers are "optional, randomly named, auto-removed" | `create_agent` always creates one; naming is `orchestrator-agent-<provider>-<uuid4[:12]>`; `auto_remove=True` (`:151`). A sandbox may legitimately have none. `replace_agent` (`:246-273`) already exists as stop-then-recreate. | Decision 3's "replaceable main agent" has an existing mechanism to build on. |
| A10 | Dependency volumes are `ro` in preview containers (ADR 0003) | `rw` at `previews/service.py:1124` and `:1205`, with the Vite rationale at `:1201-1204`. `ro` only for agents (`agents/service.py:174-177`). | ADR 0003 overstates its invariant. Out of scope here; noted so no phase relies on it. |
| A11 | ADR 0005 claims remotes and hooks are stripped | No code strips remotes anywhere. `core.hooksPath=/dev/null` appears only twice, both in `delegation/delivery.py:574,583`. `.git` is **not** in `EXCLUDED_DIRECTORY_NAMES` (`projects/service.py:61-83`), so host remotes are copied into every sandbox. | Phase 4 makes ADR 0005 true for v1 clones. |
| A12 | Three `_run_git` copies | Four container-git call sites: `tasks/service.py:903`, `delegation/delivery.py:669`, `projects/service.py:906` (inline), `previews/service.py:1422` (inline `_export_commit`). All four are `network_disabled=True`. | Phase 3 consolidates four, not three. And **every existing git path is network-disabled**, so the canonical fetch in Phase 4 needs a new, separate, network-enabled execution mode. |
| A13 | 37 `mysql` lines in three files | Verified exactly: `previews/service.py` 31, `previews/detection.py` 5, `previews/models.py` 1. | Phase 6 scope estimate stands. |
| A14 | Preview row is written after Docker | Confirmed: `_start_native` at `previews/service.py:443`, `create_preview_run` at `:544`. There is a compensating cleanup at `:545-559`, but a process crash still orphans resources. | Phase 5 fixes as proposed. |

## 1.3 Other verified facts the plan depends on

* Controller DB: `backend/.controller-data/controller.sqlite3` (`controller/config.py:7,18`),
  overridable by `CONTROLLER_DATA_DIRECTORY`. It currently holds **1 project, 1 sandbox** —
  small enough that the `projects` rebuild can be validated exhaustively, and real backups
  already exist in that directory.
* The schema-shape guard is `tests/controller/test_store.py:33
  test_initial_migration_creates_the_current_schema_once`. Every schema phase updates it.
* Test harness: one 24-line `tests/conftest.py` with a single autouse fixture pointing the
  store at `tmp_path`. **There is no shared fake Docker client** — roughly 15 files hand-roll
  their own `StubDockerClient`. Real-Docker tests are opt-in via `RUN_DOCKER_PREVIEW_TESTS=1`.
  Consequence: `_start_native`, `_run_initialization`, `_attach_shared_database`, and
  `_run_shared_sql` have **no default-run coverage**.
* Frontend keys projects by name: `fetchProject(projectName)`, `removeProject(projectName)`,
  and `createProjectSandbox` posts `{path}` only (`frontend/src/api/projects.ts`). Planning
  routes are `/projects/{project_name}/planning` (`planning/router.py:33`) although planning
  sessions already carry `sandbox_id`.
* Shared infrastructure survival already works: `remove_project` only sweeps project-scoped
  resources when no sibling sandbox shares the `project_id` (`projects/service.py:321-326`).

## 1.4 Prerequisites before coding

1. **Back up** `backend/.controller-data/controller.sqlite3`, and keep the pre-squash
   backup (stamped 1..17) as a permanent migration-test fixture. Both are needed for the
   Phase 1 tests.
2. **Add a shared Docker test double.** Fifteen hand-rolled stubs cannot express lease,
   admission, and crash-recovery scenarios consistently. Promote one stub into
   `tests/conftest.py`. This is a prerequisite for Phase 5, not optional cleanup.
3. Decide the git image for network-enabled fetches (Phase 4) and where controller GitHub
   read credentials come from. They must not reach any agent credential volume.

## 1.5 Blockers

**None.** No repository finding makes safe implementation impossible. The two surprises
(migration version numbering, `tasks.base_commit NOT NULL`) both have small, stated
resolutions — start at version 18, and use the existing empty-string sentinel.

---

# 2. Dependency map

```
Phase 1  ordered migrations + schema
   │      (nothing that adds a column is safe before this)
   ├──────────────► Phase 2  identity, manifest fields, feature_key, ID routes
   │                   │
   │                   ├──► Phase 4  canonical mirror + independent clones
   │                   │        │
   │  Phase 3  git executor ────┘  (consolidate before adding a fetch mode)
   │                   │
   │                   ▼
   │                Phase 5  writer admission → lifecycle lease → mirror lock
   │                         → converging create / resume / destroy
   │                            │
   │                            ▼
   │                         Phase 6  per-sandbox DB, engine confirm, reset-db
   │                            │
   │                            ▼
   │                         Phase 7  staleness + sync
   │                            │
   │                            ▼
   │                         Phase 8  publish (push + PR)
   │                            │
   │                            ▼
   │                         Phase 9  manifest-driven cleanup + orphan report
```

Why each edge exists:

* **1 → everything.** `INITIAL_MIGRATION` is all `CREATE TABLE IF NOT EXISTS`. Adding a
  column to the DDL does nothing to an existing database. Any phase that reads a new column
  would read a column that does not exist.
* **1 → 2.** `projects` needs a table rebuild (drop `source_path NOT NULL UNIQUE`), which
  SQLite `ALTER TABLE` cannot do.
* **3 → 4.** Four git executors, all network-disabled. Consolidate first, then add exactly
  one network-enabled fetch mode in one place, so credential handling has a single site.
* **2 → 4.** The mirror is keyed by normalized remote; the clone is named from the sandbox
  ID. Both come from Phase 2.
* **5a → 5b.** A lease cannot exclude writers that have not yet recorded themselves. Every
  writer start must claim its row atomically *before* touching sandbox Git or Docker.
* **5 → 6.** `reset-db` is a lifecycle mutation and must take the lease.
* **6 → 7.** Sync can end in `database_failed`, and `reset-db` is the only exit. Shipping
  sync first would create a state with no recovery.
* **4 + 7 → 8.** Publish needs a reliable feature-branch state and known base commits.
* **2 + 5 → 9.** Manifest-driven cleanup needs manifest identity and ownership labels.

---

# 3. Detailed implementation phases

---

## Phase 1 — Ordered SQLite migrations and required schema changes

### Goal
The controller database can evolve. Adding a column to an existing installation works, is
idempotent, is versioned, and is testable against a real historical database.

### Why this phase comes here
Nothing depends on it conceptually, but everything depends on it mechanically. Phases 2, 5,
6, 7, and 8 all add columns. Without a runner they would add columns that appear only in
brand-new databases and are silently missing everywhere else.

### Current behavior
* `INITIAL_MIGRATION` (`store.py:13-457`) is one string, executed by
  `ControllerStore.initialize` (`:468-475`) via `connection.executescript`, followed by
  `INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)`.
* Every statement is `CREATE TABLE IF NOT EXISTS` / `CREATE [UNIQUE] INDEX IF NOT EXISTS`,
  preceded by `PRAGMA foreign_keys = ON`.
* `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)` exists
  (`:16-19`) but **nothing ever reads `version`**.
* An ordered runner existed at `bd8d06b^` and was removed by `bd8d06b`.
* `projects` is `id TEXT PRIMARY KEY, source_path TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL`
  (`:21-25`).
* `_connection` (`:476-487`) opens WAL and `foreign_keys = ON` per connection, wrapped in an
  `RLock`.

### Changes
1. Reintroduce the removed runner shape in `controller/store.py`:
   * `_add_column(connection, table, column, ddl)` — swallow only `duplicate column name`.
   * `MIGRATIONS: Mapping[int, Callable[[sqlite3.Connection], None]]`.
   * `_apply_migrations(connection)` — read applied versions, apply missing in sorted order,
     stamp each inside the same transaction.
2. **Start numbering at 18.** Add a module constant `FIRST_V1_MIGRATION = 18` with a comment
   naming the reason: pre-squash databases are stamped 1..17 with the same effective schema,
   so versions 2..17 must remain permanently reserved.
3. Call `_apply_migrations` from `initialize` after `executescript` and the version-1 stamp.
4. Add a guard test-support helper `applied_versions()` on the store for assertions.
5. Migration **18 — `sandboxes` additive columns.** All nullable, no defaults that change
   existing semantics:
   `lifecycle_version TEXT` (`legacy` | `v1`), `feature_key TEXT`, `feature_title TEXT`,
   `desired_state TEXT`, `lifecycle_status TEXT`, `operation TEXT`, `operation_phase TEXT`,
   `last_error TEXT`, `base_ref TEXT`, `created_base_commit TEXT`, `current_base_commit TEXT`,
   `pending_base_commit TEXT`, `feature_branch TEXT`, `agent_provider TEXT`,
   `network_policy TEXT`, `db_engine TEXT`, `db_name TEXT`, `schema_baseline_hash TEXT`,
   `db_data_volume TEXT`, `publish_remote TEXT`, `remote_branch TEXT`,
   `pr_requested INTEGER NOT NULL DEFAULT 0`.
6. Migration **19 — backfill.** `UPDATE sandboxes SET lifecycle_version = 'legacy',
   desired_state = 'active' WHERE lifecycle_version IS NULL`. Every pre-existing sandbox
   becomes legacy by definition. `lifecycle_status` stays `NULL` for legacy rows; NULL means
   "not lifecycle-managed".
7. Migration **20 — `projects` table rebuild.** The only destructive migration:
   ```
   PRAGMA foreign_keys = OFF
   BEGIN
   CREATE TABLE projects_new (
       id TEXT PRIMARY KEY,
       source_path TEXT,                      -- now nullable
       remote_url TEXT,                       -- normalized, credential-stripped
       default_branch TEXT,
       mirror_volume TEXT,
       created_at TEXT NOT NULL
   )
   INSERT INTO projects_new(id, source_path, created_at) SELECT id, source_path, created_at FROM projects
   DROP TABLE projects
   ALTER TABLE projects_new RENAME TO projects
   CREATE UNIQUE INDEX projects_source_path ON projects(source_path) WHERE source_path IS NOT NULL
   CREATE UNIQUE INDEX projects_remote_url  ON projects(remote_url)  WHERE remote_url  IS NOT NULL
   COMMIT
   PRAGMA foreign_key_check
   PRAGMA foreign_keys = ON
   ```
   `id` values are copied verbatim — this is what preserves `sandboxes.project_id`.
8. Add `_apply_migrations` failure handling: a failing migration must not stamp its version,
   and the exception must surface at startup rather than being swallowed.
9. **Each migration must run in its own transaction, committed before the next begins.**
   Found by testing the runner, not by reading it: `ControllerStore._connection` yields inside
   `with connection:`, and Python's sqlite3 opens a transaction implicitly before the first DML
   statement. The version stamp is an `INSERT`, so once migration 18 is stamped the connection is
   inside a transaction for every migration after it. **`PRAGMA foreign_keys = OFF` is a no-op
   inside a transaction and does not raise** — SQLite ignores it silently. Migration 20's rebuild
   would then run with foreign keys still enforced against `sandboxes.project_id`.
   Verified empirically: a migration running after a stamped predecessor observes
   `in_transaction = True` and `PRAGMA foreign_keys` still `1` immediately after setting it `OFF`.
   The runner must therefore `commit()` between migrations so a rebuild migration starts in
   autocommit. `PRAGMA defer_foreign_keys = ON` is the alternative that works inside a
   transaction, but per-migration commits are also what makes the recovery rule below true.

### Data/schema changes
* **Tables affected:** `sandboxes` (additive), `projects` (rebuilt), `schema_migrations` (rows).
* **Columns removed:** none.
* **Constraints changed:** `projects.source_path` loses `NOT NULL` and its table-level
  `UNIQUE`, regaining uniqueness as a partial index that tolerates NULL.
* **Indexes added:** `projects_source_path`, `projects_remote_url` (both partial).
* **Migration ordering:** 18 → 19 → 20. The rebuild is last so a failure leaves the additive
  work already stamped and re-runnable.
* **Data backfill:** migration 19 only.
* **ID preservation:** `projects.id` and `sandboxes.project_id` must be byte-identical after
  the rebuild. This is the single highest-risk item in the plan.
* **Backup:** take a copy of `controller.sqlite3` before first run. Document it; do not
  automate it in this phase.
* **Compatibility:** `register_sandbox`'s `ON CONFLICT(source_path)` clause keeps working
  because the partial unique index still backs it for non-NULL paths. Verify this explicitly —
  SQLite upsert conflict targets resolve against indexes, and a partial index is a valid target
  only when the statement's `WHERE` matches. If it does not resolve, the legacy insert must
  become an explicit `SELECT`-then-`INSERT`/`UPDATE` inside the same transaction.

### API/model changes
None. No route, request, or response changes in this phase. New columns are written by
nobody yet.

### Runtime/container changes
None.

### Writer/admission behavior
`initialize` is a process-startup operation, not a sandbox operation. It participates in no
lease. It runs before any writer can exist.

### Failure and recovery behavior
* A crash between two migrations leaves earlier ones stamped and later ones unstamped;
  the next start resumes. This is why each migration stamps inside its own transaction.
* The `projects` rebuild is wrapped in one transaction with `foreign_keys = OFF`. A crash
  mid-rebuild rolls back to the old table, and version 20 stays unstamped.
* `PRAGMA foreign_key_check` after the rebuild must be asserted, not merely run — a silent
  orphan would surface much later as a sandbox pointing at a missing project.
* There is no downgrade path and none is planned. Recovery from a bad migration is restoring
  the backup.

### Security boundary
No credentials, no containers, no network. The controller process is the only actor.

### Backward compatibility
* Existing sandboxes become `lifecycle_version = 'legacy'` and keep every current behavior.
* `status`, `baseline_commit`, and `dirty_baseline_json` are untouched.
* All five `register_sandbox` call sites keep working unchanged.

### Tests
* `tests/controller/test_store.py` — update
  `test_initial_migration_creates_the_current_schema_once` for the new columns and indexes,
  and assert `applied_versions() == [1, 18, 19, 20]` on a fresh database.
* New: applying migrations twice produces the identical schema and no duplicate stamps.
* New: **upgrade a copy of the real controller database** (`.controller-data/controller.sqlite3`)
  and assert the project ID `b93d2d4ce7785c1ab29aa123879db70d` and the sandbox ID
  `427faaacc4e548a4804994f83096049a` are unchanged, and `sandboxes.project_id` still resolves.
* New: **upgrade a copy of the pre-squash backup** (stamped 1..17) and assert 18/19/20 apply
  exactly once and 2..17 are never re-run.
* New: a mid-rebuild failure (inject an exception) leaves the old `projects` intact and
  version 20 unstamped.
* New: `PRAGMA foreign_key_check` returns empty after the rebuild.
* New: legacy backfill sets `lifecycle_version='legacy'` for every pre-existing row.

### Completion criteria
* Upgrading a copy of the live controller database succeeds with no data loss.
* Upgrading the pre-squash 1..17 database applies only 18, 19, 20.
* Running migrations twice produces the same final schema and the same version list.
* Existing project and sandbox IDs are byte-identical after the rebuild.
* `foreign_key_check` is empty.
* The whole existing suite still passes.

### Not included yet
No column is read or written by application code. No identity change. No `feature_key`
requirement. No lifecycle semantics — `lifecycle_status` exists and stays NULL.

---

## Phase 2 — Manifest fields, deterministic identity, naming, and ID-based routes

### Goal
A v1 sandbox has a deterministic, content-derived identity from `(project, feature_key)`;
resources are named from it; and the API can address a sandbox by ID instead of by display
name. Repeating a create request converges on the same sandbox.

### Why this phase comes here
Depends on Phase 1 for the columns and the `projects` rebuild. Phase 4 needs a stable
sandbox ID for clone and volume names; Phase 5 needs it for lease rows and ownership labels;
Phase 9 needs it for the `sbx-*` sweep.

### Current behavior
* `project_id = uuid5(NAMESPACE_URL, f"orchestrator-project:{source_path}").hex`
  (`projects/service.py:178`, helper at `:960`) — deterministic but keyed on the **host path**,
  so two checkouts of one remote are two unrelated projects.
* `sandbox_id = uuid4().hex` (`:179`) — **random**.
* Display name comes from `_next_sandbox_name` (`:550`), scanning Docker volume labels under
  the process-local `_sandbox_creation_lock` (`:60`) and picking the next `-sandbox-N`.
* Volume name is `orchestrator-project-<slug>-<sha256(source_path\0name)[:10]>` (`:583`),
  plus a `-controller-metadata` sibling (`:591-597`).
* Identity of record is Docker labels: `list_registered_projects`,
  `inspect_registered_project`, and `controller/lifecycle.py:155-192` all read labels.
* Routes are name-keyed: `GET/DELETE /projects/{project_name}`, and planning is
  `/projects/{project_name}/planning` (`planning/router.py:33`).
* `POST /projects` takes `{path}` only.

### Changes
1. New module `app/sandboxes/naming.py` — the single source of resource names:
   ```python
   sandbox_id = uuid5(NAMESPACE_URL, f"{project_id}:{feature_key}").hex   # note .hex
   short_id   = sandbox_id[:12]                                          # Docker names only
   ```
   plus `workspace_volume`, `agent_container`, `network`, `database_name`, `db_data_volume`,
   `mirror_volume`, `feature_branch` helpers producing
   `sbx-<sandbox_id>-ws`, `sbx-<sandbox_id>-agent`, `sbx-<sandbox_id>-net`,
   `sbx_<sandbox_id>`, `sbx-<sandbox_id>-db`. The **full hex** is the identity stored in the
   manifest and labels; `short_id` appears only where Docker name length forces it.
2. New module `app/sandboxes/manifest.py` — typed read/write over the Phase 1 columns, and
   the only place that maps a row to a manifest object. It must never write `sandboxes.status`.
3. Remote normalization in `app/projects/`:
   * lowercase host, strip trailing `.git`, normalize `git@host:o/r` and `https://host/o/r`
     to one canonical form;
   * **strip userinfo before hashing and before persisting** — a token in a remote URL must
     never reach the database or an event payload;
   * `project_id_for_remote(remote) = uuid5(NAMESPACE_URL, f"repo:{normalized}").hex`.
4. A **separate v1 store path** for project registration keyed on `remote_url`. It must not
   share an upsert with `register_sandbox`'s `ON CONFLICT(source_path)` clause, or a legacy
   import would reassign a v1 project's ID.
   **Evidence that the shared upsert is already unsafe**, found while verifying the Phase 1
   rebuild: `register_sandbox`'s `DO UPDATE SET id = excluded.id` is effectively dead code on
   the legacy path, because every caller derives `project_id = uuid5(source_path)`, so the
   incoming ID always equals the stored one and the UPDATE is a no-op. If it ever *did* fire —
   which is exactly what a v1 caller passing a remote-derived ID would cause — it would change
   `projects.id` out from under `sandboxes.project_id` and raise `FOREIGN KEY constraint failed`.
   Reproduced directly against an upgraded database copy. So the two store paths must be
   separate for correctness, not merely for tidiness, and the v1 path must never reach this
   upsert.
5. `feature_key` validation: required for v1 create, immutable thereafter, pattern
   `^[a-z0-9][a-z0-9-]{1,63}$`. `feature_title` is separate and mutable.
6. New v1 create request/response models. The legacy `POST /projects {path}` path stays
   exactly as it is.
7. ID-based routes added **alongside** name routes:
   `GET /sandboxes`, `GET /sandboxes/{sandbox_id}`, `DELETE /sandboxes/{sandbox_id}`.
   Name routes remain and resolve to IDs internally. Nothing is removed in this phase.
8. Ownership labels on every v1 resource: `orchestrator.sandbox.id=<full hex>`,
   `orchestrator.project.id`, `orchestrator.lifecycle.version=v1`. Resource lookup validates
   labels; a matching name with wrong labels is a hard error, never a reuse.
9. Frontend: add `feature_key` to the v1 create form and start using `sandbox_id` where a
   route already exposes it. Full route migration is Phase 9's cleanup, not this phase's.

### Data/schema changes
No new DDL. This phase gives the Phase 1 columns their meaning:
`lifecycle_version`, `feature_key`, `feature_title`, `desired_state`, plus `projects.remote_url`,
`projects.default_branch`, `projects.mirror_volume`.

Preservation requirement: **existing sandbox IDs are never recomputed.** A legacy sandbox
keeps its `uuid4` ID forever. Deterministic derivation applies only to newly created v1
sandboxes. Any code tempted to "recompute and fix" an ID is a defect.

### API/model changes
* **New request:** v1 create takes `{remote_url | project_id, feature_key, feature_title?, agent_provider?}`.
  `feature_key` is required. There is no server-side default and no slug-from-title fallback —
  Decision 2 makes the human the source.
* **New responses:** sandbox manifest view including `sandbox_id`, `feature_key`,
  `lifecycle_version`, `desired_state`, `lifecycle_status`, both base commits, `feature_branch`.
* **Idempotency:** repeating v1 create with the same `(project_id, feature_key)` returns the
  existing sandbox and its current lifecycle status. It never creates a sibling and never 409s
  merely because the sandbox exists.
* **Planning is unchanged.** `planning/service.py:66 create_session` already calls
  `ensure_sandbox_registered(project_name)` and stores `sandbox_id`. Decision 2 is satisfied by
  the existing ordering; this phase only adds an ID-addressed alias route. **Planning must not
  gain the ability to create a sandbox** — that is the "second sandbox" risk.

### Runtime/container changes
No container behavior changes yet. Naming helpers exist but nothing creates `sbx-*` resources
until Phase 4/5.

### Writer/admission behavior
None yet. Create is not converging in this phase — only its identity is deterministic.

### Failure and recovery behavior
* A crash after inserting the manifest row and before any resource exists leaves a row with
  `lifecycle_status = 'creating'` and no resources. Phase 5's `resume` handles it. Until then,
  such a row is inert and visible.
* Deterministic identity is what makes retry safe: the retry finds the same row rather than
  making a second one.

### Security boundary
* Credential-bearing remote URLs must be rejected or stripped before persistence, logging, or
  event payloads. Add an explicit test.
* No new credentials are introduced in this phase.

### Backward compatibility
* Legacy sandboxes keep `uuid4` IDs, `-sandbox-N` names, name routes, and the copy-based
  create path.
* `lifecycle_version` distinguishes them permanently. Nothing infers v1 from shape.
* Name routes continue to work for both.

### Tests
* `tests/projects/test_router.py` — legacy create still works with `{path}` and produces a
  `uuid4` sandbox ID.
* New `tests/sandboxes/test_naming.py`: `uuid5(...).hex` is used (a regression guard against
  the `UUID` subscripting bug named in the proposal); names are stable; `short_id` appears only
  in Docker names.
* New: identical `(project_id, feature_key)` yields an identical `sandbox_id`; a different
  `feature_key` yields a different one.
* New: remote normalization maps the three URL forms to one `project_id`; userinfo is stripped;
  a URL with a token never appears in the stored row or any event.
* New: `feature_key` is rejected when missing or malformed on v1 create; a second create with
  the same key returns the same sandbox rather than a sibling.
* New: legacy import cannot reassign a v1 project's ID (the two store paths do not collide).
* `tests/planning/test_service.py` — a planning session created after sandbox creation attaches
  to that sandbox; **assert no sandbox is created by planning**.
* New: resource lookup refuses a name match with wrong ownership labels.

### Completion criteria
* Repeated v1 create with the same project and `feature_key` resolves to the same sandbox.
* A legacy sandbox's ID is unchanged after this phase ships.
* Planning starts after sandbox creation, attaches to that sandbox, and creates nothing.
* Both name routes and ID routes resolve the same sandbox.
* No credential-bearing remote is ever persisted.

### Not included yet
No mirror, no clone, no lease, no database, no sync, no publish. v1 create records intent and
identity; it does not yet provision anything.

---

## Phase 3 — Consolidate Git execution

### Goal
One hardened git executor. Every git container in the codebase goes through it, so Phase 4 can
add network-enabled fetching in exactly one place with one credential path.

### Why this phase comes here
Behavior-preserving and independent of Phases 1–2, so it can land early and in parallel. It
must precede Phase 4: adding a network-enabled mode to four duplicated call sites would create
four places where a GitHub token could leak.

### Current behavior
Four container-git call sites, verified:

| Anchor | Mounts | Hardening |
|---|---|---|
| `tasks/service.py:903-919 _run_git` | `{volume: /project rw}` | `remove`, `network_disabled`, `cap_drop ALL`, `no-new-privileges`, tmpfs 32m. **No `read_only`, no env.** Callers `:117, :425, :501, :575` |
| `delegation/delivery.py:669-692 _run_git` | caller-supplied dict; `_merge_source` mounts host source **rw** | as above **plus** `read_only=True` and `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, `HOME=/tmp`. Callers `:358, :537, :586` |
| `projects/service.py:906-931 ensure_git_baseline` (inline) | `{volume: /project rw}` | as tasks'; also `_ensure_git_image` pull (`:934-938`) |
| `previews/service.py:1422-1461 _export_commit` (inline) | `{project: /source ro, workspace: /workspace rw}` | `read_only=True`, rest as above |

All four use `entrypoint=["sh","-c"]`, `alpine/git:latest` by default
(`previews/config.py:19,53`), run as root, and are `network_disabled=True`.
`core.hooksPath=/dev/null` appears only at `delivery.py:574,583`.

### Changes
1. New `app/sandboxes/git.py` exposing one function, e.g.
   `run_git(docker_client, *, image, volumes, script, network=NetworkMode.NONE, environment=None, ensure_image=False)`.
2. Adopt the **strictest** existing configuration as the default: `read_only=True`,
   `cap_drop=["ALL"]`, `no-new-privileges`, `remove=True`, tmpfs `/tmp` 32m, and the delivery
   env block (`GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, `HOME=/tmp`).
   `GIT_TERMINAL_PROMPT=0` becoming universal is a genuine safety improvement: no git command
   can ever block on a credential prompt.
3. Verify `read_only=True` is safe for the two call sites that lack it. Their scripts write only
   into `/project` (a volume) and `/tmp` (tmpfs), so it should be. If any script writes elsewhere,
   fix the script, not the hardening.
4. Add `core.hooksPath=/dev/null` to **every** script, not just the two delivery ones. Sandbox
   `.git/hooks` is copied from the host today (`.git` is not excluded from the copy), so a hook
   can already run inside a controller-launched git container.
   **Verified empirically, not inferred:** a repository carrying `.git/hooks/pre-commit` had that
   hook execute during `GIT_BASELINE_SCRIPT`'s commit inside the real git container. The agent has
   the workspace mounted `rw` including `.git`, so this is agent-controlled code running in a
   controller-launched container. The container is `network_disabled`, `cap_drop=ALL`, and mounts
   only `/project`, so the blast radius is bounded — but ADR 0005 explicitly claims hooks are
   bypassed, and that claim is false for every path except the two in `delegation/delivery.py`.
   Also verified: adding `read_only=True` plus the `GIT_CONFIG_NOSYSTEM` / `GIT_TERMINAL_PROMPT` /
   `HOME=/tmp` env block to `ensure_git_baseline` produces a byte-identical resulting HEAD commit,
   so the tightening is safe for the two call sites that currently lack it.
5. Migrate all four call sites. Delete the two `_run_git` copies and inline the two others.
6. Keep `_ensure_git_image` as an explicit flag rather than an implicit pull on every call.

### Data/schema changes
None.

### API/model changes
None.

### Runtime/container changes
Same image, same isolation, stricter defaults. `network` becomes an explicit parameter that
defaults to no network — Phase 4 will pass a different value, and only Phase 4 may.

### Writer/admission behavior
Unchanged. This phase moves code; it does not change who may run git.

### Failure and recovery behavior
Unchanged. Exceptions propagate exactly as before; existing callers keep their own rollback
(for example `tasks/service.py:147-149` deleting the task row).

### Security boundary
* Executor: throwaway container, root inside, `cap_drop=ALL`, `read_only`, no network.
* Credentials: none. This phase introduces no credential path.
* Filesystem: only the volumes the caller passes. `delivery.py`'s host-source `rw` mount is
  preserved as-is for legacy — it is already guarded by `validated_source_path`.
* Never exposed: nothing new.

### Backward compatibility
Fully behavior-preserving except for the three hardening additions (`read_only`, env block,
`hooksPath`). Each is a tightening; each needs a test proving existing flows still pass.

### Tests
* `tests/test_git_baseline.py` — unchanged expectations, now through the shared executor.
* `tests/tasks/test_service.py` (55 tests) and `tests/delegation/test_delivery.py` (16) are the
  regression net; they must pass untouched.
* New `tests/sandboxes/test_git.py`: default kwargs assert `network_disabled`, `read_only`,
  `cap_drop`, `no-new-privileges`, and the env block; `hooksPath` is present in every script.
* New: a repository with a hostile `.git/hooks/pre-commit` does not execute it under any
  executor call.

### Completion criteria
* Exactly one container-git implementation remains in `backend/app`; a grep for
  `entrypoint=["sh","-c"]` finds one git site.
* Every existing git test passes without modification to its assertions about behavior.
* No git call site can enable networking without passing the explicit parameter.

### Not included yet
No fetch, no remote, no credentials, no mirror.

---

## Phase 4 — Canonical project mirror and independent sandbox Git clones

### Goal
A v1 sandbox is an independent clone of a controller-owned mirror, pinned to a known
`origin/main` commit, with no remotes to GitHub, no hooks, and both baseline commits recorded.

### Why this phase comes here
Needs Phase 2's identity and normalized remote, and Phase 3's single executor. Phases 7 and 8
depend on it: staleness is measured against `current_base_commit` in the mirror, and publish
pushes from the sandbox through the controller.

### Current behavior
* Sandboxes are `tar` copies: `COPY_COMMAND` (`projects/service.py:93-128`) pipes
  `tar -C /source … | tar -C /project` inside a hardened `alpine` container.
* `.git` is **not** in `EXCLUDED_DIRECTORY_NAMES` (`:61-83`), so the full object database,
  reflog, hooks, and `.git/config` — including host remotes — are duplicated per sandbox.
* No code strips remotes anywhere, contradicting ADR 0005.
* `ensure_git_baseline` (`:906`) runs `git init -b main` only when `.git` is absent, appends
  `.agent/ .claude/ .orchestrator/` to `.git/info/exclude`, and commits `sandbox baseline` if
  there is no HEAD.
* `sandboxes.baseline_commit` is "the first commit ever made, not the current one"
  (`tasks/service.py:79`) — unusable as a base pin.
* Linked worktrees and submodules have `.git` as a *file*, so the `[ ! -d .git ]` test passes,
  `git init` runs against a missing `gitdir:` target, and Git 2.50.1 exits 128. It fails closed
  with an unhelpful error.
* No network-enabled git anywhere.

### Changes
1. **Project mirror.** Controller-owned bare repository per project in a Docker volume named
   from `projects.mirror_volume`. Created on first v1 use.
   * `git clone --mirror <remote>` into the volume, or `git init --bare` + `remote add origin`
     + `fetch`. **These two are NOT interchangeable, and the difference is a silent, severe bug.**
     Found by the Phase 7 sync end-to-end test, after the first implementation had shipped:
     fetching with `+refs/heads/*:refs/remotes/origin/*` into a bare repository leaves
     `refs/heads` **empty**, so `git clone --no-local /mirror /workspace` reports *"You appear to
     have cloned an empty repository"* and cannot resolve the pinned base commit. Reproduced
     against a real upstream repo — every v1 create against a real remote produced an empty
     workspace. Use true-mirror semantics (refspec `+refs/*:refs/*`) so `refs/heads/*` exist.
     Keep these consistent with that choice: the default branch comes from the mirror's own `HEAD`
     symref, not `refs/remotes/origin/HEAD`; the pin comes from `refs/heads/<branch>`; and
     `base_ref` becomes `refs/heads/<branch>` rather than `origin/<branch>`, which both the
     staleness `rev-list` range and the sync fetch must use.
     It escaped Phase 4's own end-to-end test because that test built its mirror by **pushing**
     into it, which creates `refs/heads/main`, instead of driving the real creation path. A fixture
     that constructs state differently from production can pass while production is broken.
   * Store `projects.remote_url` (normalized, credential-free), `projects.default_branch`
     (resolved from `origin/HEAD`), `projects.mirror_volume`.
   * The mirror is never mounted into an agent, a worker, or a preview.
2. **Canonical fetch** — the one network-enabled git operation. Implemented as a single function
   in `app/sandboxes/git.py` using the explicit network parameter from Phase 3.
   **Trap to avoid, created by Phase 3:** the executor disables hooks by setting
   `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=core.hooksPath`, `GIT_CONFIG_VALUE_0=/dev/null`, and it
   applies those *after* caller-supplied environment so a caller cannot switch hooks back on. If
   the fetch supplies credentials through git config env — `http.extraHeader`, a `credential.helper`
   — it must **extend** that count to 2 and add `KEY_1`/`VALUE_1`, not redefine `GIT_CONFIG_COUNT`.
   Overwriting the count silently re-enables project hooks on the one path that holds a GitHub
   token. Add a test asserting hooks stay disabled while credentials are supplied.
   * Runs in its own container with the mirror volume mounted `rw` and **nothing else**.
   * Receives controller **read** credentials only, by the accepted mechanism below. Never a file
     inside a volume that outlives the container, and never an environment variable on the
     container.

   **Accepted v1 credential design.** The source is a controller-only environment variable,
   `ORCHESTRATOR_GITHUB_READ_TOKEN`. Per fetch: the controller reads the token from its own
   environment, writes a short-lived `0600` secret file, bind-mounts that file **read-only** into
   the fetch container at `/run/secrets/github_read_token`, git authenticates by reading it through
   `GIT_ASKPASS` or an equivalent credential helper, and the file is removed after the container
   exits. The token must never reach the repository, the sandbox workspace, the mirror, the
   controller database, Docker labels, command-line arguments, or logs. The credential-source
   interface stays small enough that a system secret store can replace the environment variable
   later without changing the git fetch API. Publish keeps a logically separate write path.

   **Host-path constraint, measured on this machine — not a detail.** Docker Desktop shares only
   certain host paths. `/tmp` and `/private/tmp` are **not** shared here: bind-mounting a file from
   them raises no error and instead materializes an **empty directory** at the mount point, so git
   fails as if the credential were wrong. `$HOME` (under `/Users`) mounts correctly for both file
   and directory binds. Therefore the short-lived secret must be written under a Docker-shared,
   controller-owned directory outside the repository — a configurable path defaulting to somewhere
   like `~/.orchestrator/run-secrets`, never `tempfile.mkdtemp()` (which lands in `/var/folders`)
   and never inside `backend/.controller-data/`, which is in the repository tree.
   Because the failure is silent, the fetch script must **verify** the secret is a regular,
   non-empty file before using it and fail loudly with a mount-specific message otherwise.
   * No workspace volume, no host path, no sandbox volume is mounted in this container.
3. **Sandbox import** — two-step, per proposal 5.5. The sandbox never talks to GitHub.
   * `git clone --no-local <mirror> <workspace>`. `--no-local` is mandatory: the agent runs as
     root with `.git` mounted `rw` (`agents/service.py:147`), so hardlinked objects would let a
     stray redirect corrupt the mirror and every sibling clone.
   * Immediately strip remotes and hooks in the clone: remove every remote or repoint a single
     remote at the mirror path; empty `.git/hooks`; assert no remote URL contains a host.
   * Create the feature branch at the pinned commit.
   * Append the `SANDBOX_SCAFFOLDING` exclusions as today.
4. **Pin both baselines.** `created_base_commit` (immutable, audit anchor) and
   `current_base_commit` (moves only on successful sync) both set from the mirror's
   `default_branch` at creation. `base_ref` records which ref was pinned.
   `sandboxes.baseline_commit` keeps its existing legacy meaning and is not reused.
5. **Reject unusable sources on the legacy import path**: detect `.git` as a file (linked
   worktree or submodule) and refuse with a message naming the linked worktree, instead of the
   raw `fatal: not a git repository`. Fix this here rather than deferring it — it is a one-line
   test in the baseline script and the proposal's open question 12 asks only *when*, not *whether*.
6. **v1 create requires a remote** (Decision 1). A folder with no `origin` is refused for v1 with
   a message pointing at the legacy path.

### Data/schema changes
No new DDL. Populates `projects.remote_url`, `projects.default_branch`,
`projects.mirror_volume`, `sandboxes.base_ref`, `created_base_commit`, `current_base_commit`,
`feature_branch`.

### API/model changes
* v1 create response gains `created_base_commit`, `current_base_commit`, `base_ref`,
  `feature_branch`.
* New read-only route: project mirror status (last fetch time, default branch, head commit).
* Errors: 400 for a project without a remote on the v1 path; 422 for a linked-worktree source
  on the legacy path.

### Runtime/container changes
| Container | Owner | Mounts | Network | Credentials |
|---|---|---|---|---|
| Canonical fetch | controller | mirror volume `rw` only | **enabled** | controller read token, ephemeral |
| Clone | controller | mirror `ro`, workspace `rw` | none | none |
| All existing git helpers | controller | unchanged | none | none |
| Agent / worker / preview | sandbox | workspace, deps, credentials | as today | provider credentials only |

The mirror volume is **project-scoped shared infrastructure**. It carries a project label and
no sandbox label, so no sandbox destroy may remove it.

### Writer/admission behavior
* Canonical fetch is a **read-only** operation with respect to sandbox state. It mutates only
  the shared mirror, and Phase 5 adds the project mirror lock for that.
* Clone and branch creation are part of `create`, a **lifecycle mutation**. In this phase create
  is not yet lease-guarded; Phase 5 adds that. This ordering is acceptable only because v1 create
  is still behind an explicit new route and not yet reachable by concurrent flows.

### Failure and recovery behavior
* Fetch failure: the mirror is left at its previous state; git's own lock discipline handles a
  partial fetch. Create fails with the manifest row in `creating` and no sandbox resources.
* Clone failure: workspace volume may exist and be empty or partial. Recovery is Phase 5's
  `resume`, which validates repository identity and recreates safe missing resources. Until
  Phase 5 lands, a failed create leaves a diagnosable row rather than a silent orphan.
* Crash between manifest write and clone: the row says `creating`; the resources may or may not
  exist. Ownership labels are what make the retry safe — a resource with the right name but the
  wrong labels is refused, never adopted.
* A partially-created feature branch is safe to recreate because the pin is recorded.

### Security boundary
This is the phase where GitHub enters the system, so the rules are absolute:

* **Which process fetches:** a dedicated controller-launched container, nothing else.
* **Which credentials:** controller **read** credentials only. Write credentials do not exist
  until Phase 8, and the two code paths stay separate so a read-only deployment is possible.
* **Which paths:** the mirror volume only. No workspace, no host path, no credential volume.
* **Network:** enabled for this container only. Every other git container stays network-disabled.
* **Never exposed:** GitHub credentials must never enter an agent credential volume, a workspace,
  an env file, a label, or an event payload. Add an explicit test asserting the agent credential
  volume contains no GitHub token.
* **Sandbox clones must not talk to GitHub.** After clone, assert zero remotes pointing at a
  network host. This assertion is a test, not a comment.

### Backward compatibility
* Legacy sandboxes keep the `tar` copy, copied remotes, copied hooks, and `baseline_commit`.
  They are not re-cloned and not stripped — stripping remotes on an existing legacy sandbox
  could break a workflow a human relies on.
* Legacy delivery (`delegation/delivery.py`) is untouched.
* The one behavior change on the legacy path is the friendlier linked-worktree refusal, which
  replaces an error with a better error.

### Tests
* New: mirror creation from a local bare repository used as a stand-in remote; second call is
  get-or-validate, not re-clone.
* New: clone uses `--no-local`. Assert the flag, and assert objects are not hardlinked (compare
  inode counts or verify writing into the clone's `.git/objects` does not affect the mirror).
* New: after clone, `git remote -v` lists no host-bearing URL and `.git/hooks` is empty.
* New: both baseline commits are recorded and equal at creation; `created_base_commit` is never
  written again.
* New: v1 create refuses a project with no remote.
* `tests/test_git_baseline.py` — add a linked-worktree source producing a named refusal rather
  than `fatal: not a git repository`.
* New: the fetch container mounts only the mirror; assert the volume dict.
* New: a token in the configured remote never appears in the stored project row or events.
* Legacy: `tests/projects/test_router.py` copy-path tests pass unchanged.

### Completion criteria
* A v1 sandbox is created from a known `origin/main` commit and both base commits are recorded.
* The sandbox clone has no GitHub remote and no hooks.
* The mirror is fetched by exactly one container, which mounts only the mirror.
* Two sandboxes of one project have fully independent `.git` directories with no shared inodes.
* Legacy create is unchanged; a linked-worktree source is refused by name.

### Not included yet
No mirror lock (Phase 5c) — concurrent fetches can still contend. No sync, no staleness, no
push, no PR. No database work.

---

## Phase 5 — Writer admission, lifecycle lease, mirror lock, and converging create/resume/destroy

### Goal
Lifecycle operations and state-mutating work exclude each other correctly. An **idle main-agent
environment does not block anything**; an **active main-agent write turn does**. Create, resume,
and destroy converge instead of duplicating or refusing.

This is the phase where Decision 3 is implemented.

### Why this phase comes here
Depends on Phase 1 (columns), Phase 2 (identity, ownership labels) and Phase 4 (resources worth
protecting). Phases 6, 7, and 8 all take the lease, so it must exist first. Within the phase the
order is strict: **5a before 5b** — a lease cannot exclude writers that have not recorded
themselves.

### Current behavior
* Four per-class partial unique indexes, no cross-class exclusion:
  `one_active_agent_per_sandbox` (`store.py:51`), `one_open_task_per_sandbox` (`:178`),
  `one_active_preview_per_sandbox` (`:77`), `one_active_delegation_per_sandbox` (`:296`),
  plus `one_running_run_per_delegation` (`:414`).
* Writers legitimately nest: `claim_run` starts a task (`delegation/execution.py:173`), and a
  task preview requires an existing task row (`previews/service.py:261-292`).
* Row-versus-resource ordering, verified:
  * `start_task` — `ensure_git_baseline` at `:82`, row at `:98-107`, mutating git after
    (`:114-146`, with the explicit "the row is the lock" comment).
  * `create_agent` — baseline `:92`, dependency volume `:124`, row `:134`, container `:147`.
  * `create_preview_run` — Docker at `:443`, row at `:544`. **Inverted.**
  * delegation — row inserted directly as `ready` at `delegation/service.py:119`.
* **"Active agent turn" has no representation at all.** The provider CLI is `exec`ed into the
  idle container (`agents/service.py:312-349`); nothing records that a turn is running.
* Reconciliation runs once at startup (`controller/lifecycle.py:114`), settles interrupted turns
  (`:30`), rejects abandoned task branches (`:81`), and back-fills sandboxes as `discovered`
  (`:177`). It audits and marks; it never recreates.
* `remove_project` (`projects/service.py:312`) takes a **name**, sweeps by label, deletes the row,
  and returns **404 on a repeat** (`:279`).
* `_sandbox_creation_lock` is a process-local `Lock` (`:60`); the backend must run one worker.

### Changes

#### 5a — Establish every writer row before sandbox work, and add main-agent writer sessions
1. **Main-agent writer session (new, required by Decision 3).**
   * New table `agent_writer_sessions(id, sandbox_id, agent_run_id, kind, started_at, ended_at, heartbeat_at)`
     with a partial unique index on `sandbox_id WHERE ended_at IS NULL`.
   * **Starts** when a write-capable main-agent turn begins: the human sends a prompt through
     `start_agent_exec` / the terminal write path, or a headless turn is dispatched.
   * **Ends** when that turn completes, fails, or is reclaimed as stale.
   * `agent_runs` status stays exactly as it is. An `agent_runs` row in `created`/`running`
     means *the environment exists*; a `agent_writer_sessions` row with `ended_at IS NULL`
     means *something is writing*.
   * **Only the writer session participates in lifecycle exclusion.** The `agent_runs` row does not.
   * A purely interactive shell attached to the container is the hard case. Take the conservative
     rule: opening a terminal that can write starts a session; detaching or closing ends it; a
     session with a stale heartbeat is reclaimable. Document that an attached idle shell counts
     as a writer, because the controller cannot see what a human types.
2. **Tasks.** Add a controller-only `preparing` status; include it in `one_open_task_per_sandbox`.
   The admission transaction inserts the `preparing` row with `base_commit = ""` (the existing
   sentinel convention already used for `base_branch` at `tasks/service.py:103` — **no `tasks`
   table rebuild needed**). Then `ensure_git_baseline` runs, base fields are filled, and the row
   moves to `open`. A preparation failure moves it to `failed`.
3. **Agents.** Move `start_agent_run` ahead of `ensure_git_baseline` and dependency-volume
   resolution. Reconcile a stale agent first. Failure moves the row to `failed` and cleans up what
   that attempt created.
4. **Previews.** Insert a `preparing` `preview_runs` row before `_start_native`. This also fixes C2.
   Failure moves it to `failed`; the existing compensating cleanup at `:545-559` stays as a
   second line of defence.
5. **Delegations.** The lease check joins the same transaction as `create_delegation_revision`
   (`delegation/service.py:113-122`).

#### 5b — Lifecycle lease
6. New table `sandbox_leases(sandbox_id PRIMARY KEY, operation, operation_id, owner, acquired_at, heartbeat_at)`.
   One row per sandbox; its primary key is the exclusion.
7. Every lifecycle mutation takes it: `create`, `resume`, `confirm-engine`, `sync`, `reset-db`,
   `publish`, `destroy`. Not just the destructive ones — `create` and `resume` provision
   resources, and a `resume` racing a `destroy` is the exact conflict the lease prevents.
8. **Atomicity.** Every admission transaction opens with `BEGIN IMMEDIATE` before reading
   coordination state.
   * *Lifecycle start:* in one transaction, check for any active writer row (open/preparing task,
     active preview, active delegation, **open agent writer session**) and insert the lease.
   * *Writer start:* in one transaction, check the sandbox admits writers and holds no lease, then
     insert the active row.
9. **Release across human waits.** `create` releases the lease when it enters
   `awaiting_engine_confirmation` (Phase 6); `confirm-engine` claims a fresh one.
10. **Destroy drains rather than refuses.** In one transaction set `desired_state='destroyed'`,
    `lifecycle_status='draining'`, and insert the lease — destroy alone may claim it while writers
    exist. Then stop existing writers while the lease blocks new ones, then `destroying`, then
    remove resources, then tombstone, then release.
11. **Stale lease reclaim.** Record `operation_id` and timestamps. Extend
    `reconcile_controller_state` to reclaim leases whose operation is already settled — it does
    exactly this for turns today (`controller/lifecycle.py:30`). `resume` may reclaim its own
    sandbox's lease on the same evidence.
12. **Refuse by default, stop on explicit opt-in.** A blocked lifecycle call returns 409 naming
    the blocking writer. An explicit flag stops a preview first, reusing
    `tasks/service.py:708 _stop_task_preview` rather than writing a second teardown path.

#### 5c — Project mirror lock
13. A project-scoped lock covering mirror creation, origin validation, and fetch only — never the
    clone and never sandbox work. Fixed lock order: **sandbox lease first, then mirror lock**, so
    the pair cannot deadlock. Persisted like the lease so a crash is reclaimable.

#### 5d — Converging create, resume, destroy
14. `create`: get-or-validate at every step, keyed by ownership labels. A resource with the right
    name and wrong labels is an error, never adopted. Phase checkpoints written to
    `operation` / `operation_phase` before each externally visible step.
15. `resume`: requires `desired_state='active'`; verifies workspace volume, repository identity,
    expected feature branch, database, network; recreates safe missing resources; preserves the
    worktree and branch; reports `degraded` when an inconsistency is unsafe to repair silently.
16. `destroy`: label-validated sweep, tombstone retained, **shared infrastructure preserved** —
    mirror volume, shared database server, dependency volumes. The existing sibling check
    (`projects/service.py:321-326`) is the precedent. A repeated destroy returns the destroyed
    result, not 404.

### Data/schema changes
* **Migration 21:** `sandbox_leases` table + reclaim columns.
* **Migration 22:** `agent_writer_sessions` table + partial unique index on
  `sandbox_id WHERE ended_at IS NULL`.
* **Migration 23:** drop and recreate `one_open_task_per_sandbox` to include `preparing`;
  drop and recreate `one_active_preview_per_sandbox` if `preparing` is not already covered
  (it is — `store.py:77-79` already lists `preparing`, so only the task index changes).
* **Migration 24:** `sandbox_tombstones(sandbox_id PRIMARY KEY, destroyed_at, reason, manifest_json)`.
* **No table rebuild.** `tasks.base_commit` stays `NOT NULL` thanks to the sentinel decision (A8).
* Backfill: none. Leases and writer sessions start empty.
* Index change ordering matters: recreate the task index in the same migration that adds
  `preparing`, or a `preparing` row could violate nothing and two could coexist.

### API/model changes
* New routes: `POST /sandboxes/{id}/resume`, `DELETE /sandboxes/{id}` (converging).
* 409 responses gain a structured body naming the blocking writer class and its ID.
* Lifecycle operations accept `stop_blocking_previews: bool = false`.
* Repeated `DELETE` returns 200 with the tombstone, not 404.
* Sandbox detail response exposes `lifecycle_status`, `operation`, `operation_phase`, `last_error`.
* New state transitions: `creating → awaiting_engine_confirmation → ready`,
  `ready → syncing → ready | database_failed`, `ready → draining → destroying → tombstone`,
  `* → degraded`.

### Runtime/container changes
* **Persistent main-agent environment.** `sbx-<id>-agent`, deterministic name, ownership labels,
  created at `create` and recreated by `resume`. `replace_agent` (`agents/service.py:246`) already
  implements stop-then-recreate and is the mechanism for replacement.
* **Durable state must not live in the container.** Verified today: the workspace volume,
  credential volume, and dependency volume hold everything, and `auto_remove=True` on the agent
  container is already safe. Phase 5 adds the explicit test: replace the main agent and assert the
  workspace, Git repository, branch, manifest, and database are unchanged.
* **Delegated workers** stay exactly as they are: one hardened container per turn
  (`tasks/runner.py:102`), force-removed in `finally` (`:167-168`). No worker pool is introduced.
* **Per-sandbox network** `sbx-<id>-net` carries the sandbox label and **no run label**, so
  `_preview_networks` (`previews/service.py:3668`, which filters on managed **and** run ID) can
  never sweep it. A preview needing the sandbox database joins as a borrowed endpoint and is
  **disconnected, not removed**, at teardown.

### Writer/admission behavior
The classification, stated explicitly:

| Operation | Class |
|---|---|
| Main-agent container exists, idle | **not a writer** |
| Main-agent write turn / attached writable terminal | **active writer** (writer session) |
| Open or preparing task | active writer |
| Delegated work-item run | active writer (via its task) |
| Active preview (`preparing|running|restarting|rebuilding|stopping`) | active writer |
| Active delegation (`ready|running|halted`) | active writer |
| Planning turn (workspace mounted `ro`, `planning/runner.py:90`) | **read-only**, not a writer |
| Staleness inspection | read-only |
| create / resume / confirm-engine / sync / reset-db / publish / destroy | lifecycle mutation |

Nesting is preserved: a delegation holds a lease-free writer session, its child task claims its
own row, and a task preview claims its own. None of them takes the lifecycle lease, so
delegation → task → preview cannot deadlock.

### Failure and recovery behavior
* **Crash holding a lease:** the lease outlives the process. Startup reconciliation reclaims any
  lease whose operation is settled or whose heartbeat is stale. Without this, one crash makes a
  sandbox permanently unusable — this is the single most important recovery rule in the phase.
* **Crash holding a writer session:** same reclaim path. A stale writer session must not
  permanently block sync.
* **Crash between manifest write and Docker:** the row records the phase; `resume` reconciles.
  This is why the row precedes the resource everywhere.
* **Crash mid-destroy:** `desired_state='destroyed'` persists, so a repeat resumes draining rather
  than restarting. Destroy is idempotent by construction.
* **Partial create:** resumable via `resume`, or diagnosable via `operation_phase` + `last_error`.
  `degraded` exists precisely so the system refuses to guess.
* No rollback guarantees are claimed for Docker operations. Ordinary exceptions already roll back
  the sandbox path (`projects/service.py:248,254`); a process crash does not, and the manifest-first
  ordering is the mitigation, not a transaction.

### Security boundary
* Leases and writer sessions are controller-owned rows. No agent or worker can write them; they
  are trusted metadata per ADR 0001.
* No new credentials.
* Destroy validates ownership labels before removing anything. **A name match is never sufficient
  proof of ownership** — this is what prevents destroying shared infrastructure.

### Backward compatibility
* Legacy sandboxes have `lifecycle_status = NULL` and take no lease. Their writer starts still get
  the reordered rows (a strict improvement) but no lifecycle exclusion.
* `sync` and `reset-db` refuse on legacy with a clear message.
* Legacy destroy keeps working via the name route and gains the tombstone, so a repeat returns the
  destroyed result instead of 404. This is a deliberate, safe behavior change on the legacy path.
* `register_sandbox` still writes `status` from all five call sites and must **never** touch
  `lifecycle_status`. Add a test asserting a task start does not overwrite `destroying`.

### Tests
* New `tests/sandboxes/test_admission.py`:
  * an **idle** main-agent container does not block `sync` (the core Decision 3 test);
  * an **active** main-agent writer session does block `sync`, with 409 naming it;
  * a writer session with a stale heartbeat is reclaimed and stops blocking.
* New: lease excludes a second lifecycle operation; two concurrent lifecycle starts produce
  exactly one winner (run against real SQLite, which the harness already uses).
* New: writer start is refused while a lease is held; lease start is refused while a writer exists.
* New: check-and-insert is atomic — simulate interleaving and assert no double admission.
* New: **nested delegation → task → preview** completes without deadlock.
* New: destroy drains — it acquires the lease with a writer present, stops it, and completes.
* New: repeated destroy returns the tombstone; repeated create returns the same sandbox.
* New: crash recovery — a lease row with a settled operation is reclaimed at startup; assert the
  sandbox is usable afterwards.
* New: **main-agent replacement preserves state** — replace the container, assert workspace,
  branch, HEAD, manifest, and database are unchanged.
* New: **delegated workers disappear** — after a work-item run settles, no container carries the
  task label. Extend `tests/delegation/test_execution.py`.
* New: preview `preparing` row exists before any Docker call (assert ordering with the stub).
* New: task `preparing` row uses `base_commit = ""` and moves to `open`; a preparation failure
  moves it to `failed` and frees the sandbox.
* New: destroy preserves the mirror volume, the shared database server, and dependency volumes.
* New: `register_sandbox` does not overwrite `lifecycle_status` (drive it from
  `tasks/service.py:68` and `previews/service.py:3837`).
* Update `tests/controller/test_lifecycle.py` for lease and writer-session reclaim.

### Completion criteria
* An idle main-agent environment does not block `sync`.
* An active main-agent write turn does block `sync`, and the 409 names it.
* Replacing the main-agent container loses no project state.
* Delegated workers disappear after task completion.
* A preview cannot start while a lifecycle lease excludes new writers.
* Delegation → task → preview still works end to end.
* A repeated destroy returns the destroyed result instead of 404.
* A repeated create resolves to the same sandbox.
* A killed backend holding a lease recovers on restart without manual intervention.
* Destroy never removes the mirror, the shared database server, or dependency volumes.

### Not included yet
No database provisioning, no `reset-db`, no engine confirmation (the
`awaiting_engine_confirmation` state is defined but unreachable until Phase 6). No sync, no
publish, no orphan sweep.

---

## Phase 6 — One database per sandbox, engine detection/confirmation, and `reset-db`

### Goal
A sandbox owns one database for its whole life. Its engine is detected, proposed, and confirmed
by a human when uncertain. Project-defined migration and seed commands run **inside a restricted
sandbox runtime**, never on the controller. `reset-db` rebuilds the database and is the recovery
path Phase 7 depends on.

### Why this phase comes here
Depends on Phase 5: `confirm-engine` and `reset-db` are lifecycle mutations and take the lease,
and `reset-db` must remove the active writer before terminating connections. Phase 7 depends on
this phase, because sync can end in `database_failed` and `reset-db` is the only exit.

### Current behavior
* The database belongs to a **preview run**, not a sandbox. MySQL protocol only.
* `PreviewServiceType` (`previews/models.py:53-55`) has exactly one member, `MYSQL`.
  `PreviewConfiguration.validate_mode_settings` (`:145-195`) hard-codes the coupling: only a
  service key named `database`, only `DATABASE_URL` as a `from_service` variable, and
  *"Initialization commands require a database service"* (`:193-194`).
* Only engine detector: `_mysql_prisma_schema` (`detection.py:539-553`).
* Only command source in detection: `_native_dependencies` (`detection.py:506-536`) hard-codes
  `npx prisma migrate deploy` (`:517`) and adds `npm run db:seed:preview` when the script exists
  (`:518-519`).
* Modes: isolated (per-run container), `shared_server`, `shared_data`.
  Lifetimes differ: isolated+ephemeral is run-scoped; isolated+persistent is already
  sandbox-scoped (`service.py:3608-3611`); shared server is project-scoped and deliberately
  unlabeled so teardown cannot sweep it (`:1797-1802`).
* Migrations run at preview start, two call sites, both inside `_start_native`:
  `service.py:1244-1261` (shared) and `:1342-1359` (isolated), both gated on
  `if config.initialize.commands`. Guests are skipped (`:1236-1242`).
* Execution container: `_run_initialization` (`:2614-2677`) — app runtime image, `read_only=True`,
  `cap_drop=ALL`, `no-new-privileges`, `pids_limit=256`, on the run network which is
  `internal=True` for isolated access (`:3457`), env limited to `DATABASE_URL` + project secrets.
* `_shared_database_names` (`:1766-1773`) keys the shared server on the **project alone** — a
  project that changes engine collides with its own server container and data volume.
* `_run_shared_sql` (`:1988-2034`) runs admin SQL as root via the `mysql` client, with the root
  password in `MYSQL_PWD`; its docstring states root never reaches an application container.
* Measured coupling: 37 `mysql` lines — 31 in `previews/service.py`, 5 in `detection.py`, 1 in
  `models.py`.
* `git archive` in `_export_commit` (`:1422-1461`) exports only tracked content, which is why an
  in-workspace SQLite file cannot work for task previews.

### Changes
Sub-ordered per proposal 8.9.

**6a — Engine protocol (mechanical, behavior-preserving).**
1. New `app/sandboxes/database.py` with five operations: `provision`, `connection_url`,
   `run_migrations`, `drop`, and a `supports_template` capability flag.
2. Extract the existing MySQL implementation behind it unchanged. Bounded by the 37 lines above.
3. Fix the engine-collision defect: add the engine to `_shared_database_names`' key.

**6b — Detection, proposal, confirmation.**
4. Generalize `_mysql_prisma_schema` into a provider-returning detector, and add the signal
   ladder in precedence order (explicit configuration beats dependency presence):
   Prisma `provider` → `DATABASE_URL` scheme in `.env`/`.env.example` → Django `DATABASES.ENGINE`,
   Rails `config/database.yml`, Alembic `sqlalchemy.url` → `docker-compose.yml` service images →
   package dependencies (`pg`, `mysql2`, `better-sqlite3`, `psycopg`, `asyncpg`, `PyMySQL`).
5. **Conflicting signals never guess.** Surface every signal and enter
   `awaiting_engine_confirmation`.
   **Precedence must actually resolve, or the rule collapses into "always ask".** Found by testing
   the first implementation: it recorded a precedence rank on every signal, sorted by it, then
   decided with `len({s.engine for s in signals}) == 1`, ignoring rank entirely. A Prisma schema
   explicitly declaring `postgresql` was therefore overruled into ambiguity by an incidental
   `mysql2` entry in `package.json`. Stale driver dependencies are common, so this would stall
   almost every sandbox on a human gate.
   The resolution rule is two-band, matching "explicit configuration beats dependency presence":
   * **Explicit** — Prisma provider, `DATABASE_URL` scheme, Django/Rails/Alembic config (ranks 1-3).
   * **Inferred** — compose images, package dependencies (ranks 4-5).
   If any explicit signal exists, decide from explicit signals alone and ignore inferred ones
   entirely; propose when they agree, conflict when they disagree. With no explicit signal, decide
   the same way among inferred signals. A monorepo or mid-migration project with two disagreeing
   *explicit* configs is still a genuine conflict and still asks — that is the case the proposal
   cares about, and it is preserved.
6. New table `sandbox_engine_detections`. Per Adjustment B, the command fields are a **resolved,
   approved snapshot**, not controller-owned configuration:
   `sandbox_id`, `signals_json`, `proposed_engine`, `confirmed_engine`,
   `migrate_commands_json`, `seed_commands_json`, `commands_source` (`prisma`|`package_json`|
   `makefile`|`manual`), `detected_at_commit`, `actor`, `confirmed_at`.
   `detected_at_commit` is what makes the snapshot auditable: it records the project state the
   commands were read from.
7. Discovery reads **controller-read project files at a known commit**, reusing the
   `implementation_context/inventory.py` approach (*"Read the commands that a project defines
   without running project code"*, read-only mount, `network_disabled=True`). It must **not**
   read `.agent/preview.yaml` — that file is agent-writable and its precedence
   (`detection.py:198-199`) is acceptable for previews but not for sandbox-lifecycle migrations.
8. `confirm-engine` is a first-class lifecycle operation: takes sandbox ID, chosen engine, the
   approved command snapshot, and the actor; writes the detection record; claims a fresh lease;
   resumes creation from database provisioning. Without it `awaiting_engine_confirmation` is
   terminal.
9. **Never switch engines silently on sync.** Re-detect, compare, report a mismatch, and require
   an explicit human decision.

**6c — Sandbox-owned database and `reset-db`.**
10. The database belongs to the sandbox: name `sbx_<sandbox_id>` on the project's shared server,
    or `sbx-<sandbox_id>-db` volume for SQLite. Previews **consume** it rather than provisioning
    their own.
11. **SQLite lives outside the Git workspace** — in `sbx-<id>-db`, mounted at a stable path
    outside the repository tree, reached through an injected connection URL by live previews,
    task previews, agent containers, and verification containers alike. Detect a **tracked**
    database path at create time and refuse, naming the path: `.git/info/exclude` suppresses only
    untracked files, and `git archive` would omit a correctly-excluded file from task previews,
    which would then silently provision a different database.
12. `reset-db`: take the lease → refuse if any active writer exists (or stop it on explicit
    opt-in, reusing `_stop_task_preview`) → terminate connections → drop → recreate role and
    database → replay the approved migrate and seed snapshot **inside the sandbox runtime** →
    record `schema_baseline_hash` (`sha256` over sorted `(path, bytes)`; no parsing).
13. **`reset-db` finalizes a pending sync.** If `pending_base_commit` is set, after a successful
    rebuild it writes `pending_base_commit` into `current_base_commit`, clears the pending value,
    and returns the sandbox to `ready`. Against a healthy sandbox it finds no pending commit and
    only rebuilds. One extra conditional, same operation. Phase 7 depends on this.
14. **Disable `shared_data` for new v1 sandboxes.** It lets one sandbox write another's schema and
    carries real ownership complexity (`shared_database_schemas.owner_sandbox_id`, guests that
    must not migrate, revocation that must not touch the owner). It stays MySQL-and-legacy-only.

### Data/schema changes
* **Migration 25:** `sandbox_engine_detections` table, PK `sandbox_id`.
* Populates the Phase 1 columns `db_engine`, `db_name`, `schema_baseline_hash`, `db_data_volume`.
* `shared_database_schemas` (`store.py:127-139`) is unchanged and keeps serving legacy previews.
* No table rebuild. No backfill — legacy sandboxes get no detection record.

### API/model changes
* New routes: `POST /sandboxes/{id}/confirm-engine`, `POST /sandboxes/{id}/reset-db`,
  `GET /sandboxes/{id}/engine` (signals, proposal, confirmation, command snapshot).
* Create response can now return `lifecycle_status = awaiting_engine_confirmation` with the
  signal list — a normal outcome, not an error.
* `PreviewServiceType` gains `POSTGRES` and `SQLITE`. `validate_mode_settings` must relax its
  MySQL assumptions without loosening the `DATABASE_URL`-only rule.
* New transitions: `creating → awaiting_engine_confirmation → creating → ready`;
  `database_failed → (reset-db) → ready`.
* `reset-db` and `confirm-engine` return 409 on legacy sandboxes.

### Runtime/container changes
| Container | Owner | Mounts | Network | Credentials |
|---|---|---|---|---|
| Command discovery | controller | workspace **ro** | none | none |
| **Migration/seed runner** | controller-launched, sandbox-scoped | workspace `rw`, deps, sandbox DB volume | sandbox network only, no egress | sandbox DB credentials only |
| Admin SQL (`_run_shared_sql` equivalent) | controller | none | shared DB network | DB **root**, via env, never to an app container |
| Shared DB server | project-scoped shared infra | project data volume | project DB network | root |
| Preview | sandbox | as today | as today | sandbox DB URL |

The migration runner is `_run_initialization`'s shape (`previews/service.py:2614`), lifted from
preview-scope to sandbox-scope. Prefer `network_mode="none"` where the engine is SQLite, following
the `delegation/verification.py:88-96` precedent and its comment about `localhost` resolution.

### Writer/admission behavior
* `confirm-engine` and `reset-db` are **lifecycle mutations** and take the lease.
* Engine detection alone is **read-only**.
* `reset-db` must remove the blocking writer before terminating connections — terminating
  connections while a preview still runs only invites its pool to reconnect mid-drop.
* Running migrations is state-mutating but happens *inside* a lifecycle operation that already
  holds the lease; it does not take a second writer session.

### Failure and recovery behavior
* **Provisioning failure during create:** `lifecycle_status` stays `creating` with
  `operation_phase` naming the step; `resume` retries.
* **Migration failure:** `lifecycle_status = database_failed`. **Not reversible** — no Git ref
  undoes an applied migration. Recovery is `reset-db`. Do not claim otherwise anywhere in code
  comments or API messages.
* **`reset-db` failure partway:** the database may be dropped and not yet rebuilt. Stay in
  `database_failed` and allow retry. `reset-db` is idempotent by design: drop-then-recreate
  converges regardless of the starting state.
* **Crash during `confirm-engine`:** the lease is reclaimed at startup; the detection record either
  has `confirmed_engine` or does not, so a retry can distinguish an unconfirmed proposal from
  confirmed intent and never re-provisions against a guess.
* A failed migration must never leave `lifecycle_status = ready`.

### Security boundary
This is the Decision 6 phase, so the boundary is stated in full:

```
project defines migrate/seed commands   (package.json, prisma, Makefile, framework config)
        ↓  read-only, no execution
controller discovers and snapshots them (approved by a human at confirm-engine)
        ↓  orchestrates
restricted sandbox runtime executes them
```

* **Which container executes:** a controller-launched, sandbox-scoped runtime container. Never the
  controller process. Never the host.
* **Which credentials:** sandbox-scoped database credentials only. Never DB root, never GitHub,
  never provider credentials.
* **Which paths:** the sandbox workspace and the sandbox database volume. No host path, no mirror,
  no credential volume, no Docker socket.
* **Network:** the sandbox/DB network only, with no egress. The existing `internal=True` network
  (`previews/service.py:3457`) is the precedent.
* **Privileges:** `read_only=True`, `cap_drop=["ALL"]`, `no-new-privileges`, bounded pids and memory.
* **Never exposed:** controller GitHub credentials, DB root password, other sandboxes' credentials,
  the host filesystem.
* **Command provenance:** commands come from a stored snapshot approved by a human, derived from
  controller-read project files at a recorded commit — never from `.agent/preview.yaml` and never
  re-read from the live workspace at `reset-db` time.

### Backward compatibility
* Legacy sandboxes keep preview-scoped databases, `shared_data`, and MySQL. They receive no
  detection record, no `confirm-engine`, and no `reset-db` (409).
* Existing preview behavior for legacy sandboxes is unchanged, including the guest skip.
* The `shared_database_names` engine-key fix changes names for **new** servers. Existing servers
  must keep resolving — either keep the old name as a fallback lookup for legacy projects, or
  gate the new key on `lifecycle_version='v1'`. Choose the gate; it is simpler and cannot rename
  a running server.

### Tests
* `tests/previews/test_shared_database.py` — extend for the engine-keyed names; assert legacy
  names still resolve.
* `tests/previews/test_detection.py` — engine detection per signal; **conflicting signals produce
  `awaiting_engine_confirmation`, never a guess**; precedence order is respected.
* New: `confirm-engine` writes the record, claims a lease, and completes creation; without it the
  sandbox stays in `awaiting_engine_confirmation` (proving it is not terminal).
* New: `create` releases the lease on entering `awaiting_engine_confirmation`, so another
  operation is not blocked by a human wait.
* New: **migration isolation** — assert the runner container's mounts, env keys, network mode, and
  that no GitHub or root credential is present. This is the test that proves Decision 6.
* New: the controller never executes a project command — assert no `subprocess`/host execution path
  exists for the snapshot.
* New: **migration failure** sets `database_failed` and never `ready`.
* New: `reset-db` drops and rebuilds; running it twice converges; it refuses while a preview is
  active and stops it on explicit opt-in.
* New: `reset-db` finalizes `pending_base_commit` into `current_base_commit` and returns `ready`.
* New: SQLite database lives outside the workspace; `git status` in the sandbox never shows it;
  a task preview created by `git archive` reaches the **same** database.
* New: a project with a **tracked** database path is refused at create, naming the path.
* New: engine mismatch at sync is reported and never applied silently.
* New: `shared_data` is refused for v1 sandboxes and still works for legacy.

### Completion criteria
* One database belongs to the sandbox and survives preview start and stop.
* Project migration commands execute inside the sandbox runtime, not the controller — proved by an
  assertion on the container spec.
* A failed database migration enters `database_failed`.
* `reset-db` rebuilds the database and finalizes `pending_base_commit`.
* Conflicting engine signals stop at `awaiting_engine_confirmation` and `confirm-engine` exits it.
* A SQLite sandbox's database is invisible to Git and identical across live and task previews.
* Legacy previews are unaffected.

### Not included yet
No PostgreSQL template cloning (defer until `reset-db` latency is measured). No database
snapshots. No `shared_data` for new engines. No sync — the `pending_base_commit` finalization
exists but nothing sets a pending commit yet.

---

## Phase 7 — Staleness inspection and sync

### Goal
A human can see how far behind `origin/main` a sandbox is, and explicitly bring it forward. Git
failure is recoverable; database failure is not, and lands in a state with a defined exit.

### Why this phase comes here
Needs Phase 4 (mirror, base commits) and Phase 6 (`reset-db` as the recovery path, and the
`pending_base_commit` finalization). Shipping sync before `reset-db` would create a state with no
recovery. Phase 8 comes after because publish assumes a settled base.

### Current behavior
* **No sync exists.** A sandbox is a one-time snapshot (proposal 3.6, confirmed).
* No staleness computation, no `origin`, no fetch.
* `sandboxes.baseline_commit` is the first commit ever made, not a usable base.

### Changes
1. **Staleness inspection** (`GET /sandboxes/{id}/staleness`):
   * performs a canonical fetch first (which is why it needs read credentials), under the project
     mirror lock;
   * computes `git rev-list --count <current_base_commit>..<base_ref>` against the mirror;
   * **stores nothing** — staleness is informational and never triggers action;
   * returns the count **plus the mirror's fetch timestamp**, so a caller can tell a fresh answer
     from a cached one;
   * on fetch failure, degrades to the last known state, **labelled as such**, rather than
     reporting zero.
2. **Sync** (`POST /sandboxes/{id}/sync`), explicit only, never automatic:
   * take the lease; refuse if any active writer exists (409 naming it, or stop previews on
     explicit opt-in);
   * require a clean workspace;
   * create a controller **safety ref** before touching anything;
   * canonical fetch into the mirror (mirror lock, sandbox-lease-then-mirror-lock order);
   * sandbox fetches from the mirror — no network, no credentials;
   * write `pending_base_commit` **before** touching Git;
   * `lifecycle_status = syncing`;
   * **rebase before a PR exists, merge after one exists** — rebasing published history would
     force a non-fast-forward push onto an open PR;
   * replay the approved migration snapshot inside the sandbox runtime (Phase 6's runner);
   * on full success, set `current_base_commit = pending_base_commit`, clear pending, return to
     `ready`;
   * re-detect the engine and report a mismatch without applying it.
3. Sync never runs automatically on a `main` update. There is no watcher and none is planned.

### Data/schema changes
No new DDL. Uses `pending_base_commit`, `current_base_commit`, `base_ref`, `lifecycle_status`,
`operation_phase`, `last_error` from Phase 1. Add a `sandbox_safety_refs` row only if the ref name
cannot be derived deterministically — prefer a deterministic name
(`refs/orchestrator/safety/<operation_id>`) and store nothing.

### API/model changes
* `GET /sandboxes/{id}/staleness` → `{behind_count, base_ref, current_base_commit, mirror_fetched_at, stale_answer: bool}`.
* `POST /sandboxes/{id}/sync` → 202 with the operation ID; 409 when a writer blocks; 409 on legacy.
* Sandbox detail exposes `pending_base_commit` and, on failure, `last_error`.
* State transitions: `ready → syncing → ready`, and `syncing → database_failed`.

### Runtime/container changes
* Canonical fetch container (Phase 4) — network enabled, mirror only, read credentials.
* Sandbox fetch/rebase/merge containers — network disabled, workspace + mirror `ro`.
* Migration runner (Phase 6) — sandbox runtime, DB credentials only.
No new container classes.

### Writer/admission behavior
* Staleness inspection is **read-only** with respect to the sandbox. It does mutate the shared
  mirror, so it takes the **project mirror lock** but not the sandbox lease.
* Sync is a **lifecycle mutation**: it takes the lease and refuses while any writer is active —
  including an active main-agent writer session, but **not** an idle main-agent container.
* A live preview blocks sync by default: it holds database connections and bind-mounts the
  workspace, so it could observe a half-rebased tree.

### Failure and recovery behavior
This is the phase where honest limits matter most.

| Point | `current_base_commit` | `pending_base_commit` | `lifecycle_status` |
|---|---|---|---|
| Before sync | old | null | `ready` |
| Git done, migrations running | old | new | `syncing` |
| Migration failed | old | new | `database_failed` |
| After recovering `reset-db` | **new** | null | `ready` |

* **Git failure during rebase or merge:** restore from the safety ref. **Reversible.**
* **Migration failure after Git succeeded:** `database_failed`. **Not reversible.** A Git safety
  ref cannot undo applied migrations or seeds. Say this in the API error text.
* **`pending_base_commit` is what makes recovery correct.** Without it, `reset-db` would rebuild
  the database while the manifest still named the old base, and staleness would be computed from
  the wrong commit forever.
* **Crash mid-sync:** the lease is reclaimed at startup; `operation_phase` says where it stopped;
  `pending_base_commit` preserves the target. Either retry sync or run `reset-db` to finalize.
* This is acceptable only because sandbox database state outside migrations and fixtures is
  defined as ephemeral. That is why `reset-db` is a v1 operation and not a convenience.

### Security boundary
* Only the canonical fetch container touches GitHub, with **read** credentials.
* The sandbox fetches from a local volume: no remote, no network, no credentials.
* Migration replay uses the Phase 6 runner: sandbox DB credentials only, no egress.
* The safety ref lives in the sandbox repository and is controller-created; an agent may see it
  but its existence leaks nothing.

### Backward compatibility
* Legacy sandboxes have no `current_base_commit` and no mirror. `sync` and staleness return 409
  with a message pointing at explicit recreation as the path to v1.
* No legacy sandbox is converted, and nothing infers a base commit from `baseline_commit`.

### Tests
* New: staleness counts correctly after the mirror advances; the response carries the fetch
  timestamp; a fetch failure returns the last known state flagged stale, **not zero**.
* New: sync happy path — Git advances, migrations replay, `current_base_commit` moves, pending is
  cleared, status returns to `ready`.
* New: sync refuses while a preview is active and names it; the explicit opt-in stops the preview
  and proceeds.
* New: **sync does not refuse for an idle main-agent container** (Decision 3, again at this layer).
* New: **Git failure restores from the safety ref** and leaves `current_base_commit` unchanged.
* New: **migration failure sets `database_failed`**, leaves `pending_base_commit` set, and
  `current_base_commit` unchanged.
* New: `reset-db` after a failed sync finalizes the pending commit — the full recovery path,
  end to end.
* New: crash mid-sync leaves a reclaimable lease and a preserved `pending_base_commit`.
* New: rebase is used before a PR exists and merge after — assert the branch shape both ways.
* New: engine mismatch on sync is reported, never applied.
* New: sync is refused on legacy sandboxes.

### Completion criteria
* Staleness is computed after a fetch and reports its own freshness.
* A successful sync advances `current_base_commit` and clears `pending_base_commit`.
* A failed database migration during sync enters `database_failed`.
* `reset-db` finalizes `pending_base_commit` and returns the sandbox to `ready`.
* Git-only failure is restored from the safety ref.
* Sync never runs automatically.

### Not included yet
No blue-green sync, no database snapshots, no automatic sync on `main` updates, no conflict
resolution assistance. A rebase conflict fails and restores; it does not open an interactive flow.

---

## Phase 8 — Push and PR publishing

### Goal
The controller pushes the reviewed feature branch to the remote and creates or reuses a pull
request. Agents and workers never see a GitHub credential. Publishing is retryable.

### Why this phase comes here
Needs Phase 4 (remote identity, mirror) and Phase 7 (settled base commits and sync semantics).
Publishing an unsettled branch would create PRs that cannot be reasoned about.

### Current behavior
* **No GitHub integration exists.** Verified: the only `github` hits in `backend/app` are
  `implementation_context/inventory.py:39,238,247`, reading `.github/workflows/*.yml` as CI evidence.
  No push, no remote, no API client.
* Delivery is local: `capture_feature_target` (`delegation/delivery.py:59`) pins
  `(base_branch, base_commit, head_commit)` and refuses if the branch or HEAD moved;
  `merge_feature_to_source` (`:240`) requires an approved review with an exact recorded commit,
  validates the host path, mounts the sandbox `ro` and the host source `rw` (`:589-592`), and does
  `git merge --ff-only` with `core.hooksPath=/dev/null`.
* `GIT_TERMINAL_PROMPT=0` plus `network_disabled=True` make network git impossible today.

### Changes
1. New `app/sandboxes/publish.py`. Treat remote Git and the GitHub API as **two separate phases**,
   because they are two systems that fail independently.
2. **Phase one — push.** Push the feature branch from the sandbox clone to the remote. The sandbox
   must not talk to GitHub, so the path is: sandbox → mirror → controller push container with
   write credentials. Reuse the mirror as the staging point rather than granting the sandbox a
   remote.
   **Conflicts with the Phase 4 mirror config — found by the Phase 8 end-to-end push test.** The
   true-mirror fix sets `remote.origin.mirror true`, and git then refuses
   `git -C /mirror push origin refs/heads/x:refs/heads/x` with
   `fatal: --mirror can't be combined with refspecs`. Publishing exactly one branch is this phase's
   whole job, so the two settings are incompatible.
   Set only `remote.origin.fetch = +refs/*:refs/*` and do **not** set `remote.origin.mirror`. The
   fetch refspec is what puts `refs/heads/*` in the bare repo, which was Phase 4's actual
   requirement; `remote.origin.mirror` additionally makes a bare `git push origin` publish *every*
   ref, which this system never wants. `git clone --mirror` sets both, so the flag must be cleared
   after cloning.
3. **Phase two — PR.** Search for an existing PR by head branch **first**; create only if absent.
   The remote branch and the PR head branch are the idempotency anchors.
4. New table `sandbox_publications(sandbox_id, remote_branch, last_pushed_commit, remote_branch_sha,
   pr_number, pr_url, pr_state, last_error, updated_at)`. Observed results live here, separate from
   the `publish_remote`/`remote_branch`/`pr_requested` **intent** columns from Phase 1.
5. Verify the reviewed head commit before pushing, reusing the existing refuse-first shape from
   `delegation/delivery.py`.
6. `--force-with-lease` only for an intentional pre-PR rebase, **never** after a PR exists.
7. Do not mark publish complete until **both** the remote commit and the PR are verified.

### Data/schema changes
* **Migration 26:** `sandbox_publications` table, PK `sandbox_id`.
* Uses the Phase 1 intent columns. No rebuild, no backfill.

### API/model changes
* `POST /sandboxes/{id}/publish` → 202 with an operation ID; response carries branch, pushed
  commit, PR number, PR URL, PR state.
* `GET /sandboxes/{id}/publication` → the observed record.
* 409 on legacy sandboxes; 409 while a writer blocks; 424 when the remote or GitHub is unreachable.
* New transitions within the operation: `pushing → pushed → pr_pending → published`, recorded in
  `operation_phase` so a partial publish is diagnosable.

### Runtime/container changes
| Container | Mounts | Network | Credentials |
|---|---|---|---|
| Sandbox → mirror push | workspace `ro`, mirror `rw` | none | none |
| Mirror → remote push | mirror `rw` only | enabled | controller **write** credentials |
| PR creation | none (controller HTTP) | enabled | controller **write** credentials |

Read and write credential paths stay separate even if one token backs both, so a read-only
deployment is possible and publish stays the only step that can mutate the remote.

### Writer/admission behavior
Publish is a **lifecycle mutation** and takes the lease. It reads the sandbox repository and
writes the remote, so it must not race a sync or a destroy. It refuses while a writer is active,
because a task could commit between the verification and the push.

### Failure and recovery behavior
* **Push succeeded, PR creation failed:** record `last_pushed_commit` and `remote_branch_sha`, leave
  `pr_number` null, stay in a retryable phase. **A retry must not re-push and must not create a
  second PR** — it searches by head branch first.
* **Push failed:** nothing on the remote changed, or a partial ref update occurred that git itself
  rejects atomically per ref. Retry is safe.
* **A second publish reuses the existing remote branch and PR.** This is the core idempotency
  requirement.
* **Crash mid-publish:** the lease is reclaimed; `operation_phase` says whether the push completed;
  the publication record is the evidence, and the remote is re-queried rather than assumed.
* Do not claim rollback. A pushed commit is not retracted by this system.

### Security boundary
* **Which process:** controller-launched containers and the controller's own HTTP client. Never an
  agent, never a worker, never a preview.
* **Which credentials:** controller **write** credentials, only in the two publish containers.
* **Which paths:** the mirror volume only for the network push. The workspace is mounted `ro` in
  the sandbox→mirror step, which needs no credentials at all.
* **Network:** enabled only for the two publish steps.
* **Never exposed:** write credentials must never enter the agent credential volume, the workspace,
  an env file, a Docker label, or an event payload. Assert this in a test, as in Phase 4.
* The sandbox clone still has no GitHub remote after publishing — verify it afterwards, since this
  is the phase most likely to break that invariant by convenience.

### Backward compatibility
* Legacy sandboxes keep local delivery through `delegation/delivery.py` unchanged, including the
  host-source `rw` merge. Decision 4 is explicit: no automatic conversion.
* `merge_feature_to_source` is not modified, not deprecated, and not routed through publish.
* A v1 sandbox does not get the local merge path.

### Tests
* New: happy path — push then PR; the publication record holds both.
* New: **a second publish reuses the existing remote branch and PR** and creates nothing.
* New: **partial publish** — push succeeds, PR creation fails; retry creates the PR and does not
  re-push or duplicate.
* New: PR discovery by head branch finds an existing PR before creating.
* New: `--force-with-lease` is used for a pre-PR rebase and **refused** once a PR exists.
* New: publish refuses while a writer is active, and on legacy sandboxes.
* New: **no GitHub credential reaches the agent credential volume, the workspace, labels, or
  events** — assert on the container specs and the stored rows.
* New: the sandbox clone has no host-bearing remote after a publish.
* New: crash between push and PR leaves a retryable record naming the phase.
* `tests/delegation/test_delivery.py` — unchanged, proving legacy delivery still works.

### Completion criteria
* The controller publishes a reviewed branch and creates a PR.
* A second publish reuses the existing remote branch and PR.
* A failure between push and PR is retryable and produces exactly one PR.
* No agent or worker ever receives a GitHub credential.
* Legacy local delivery is untouched.

### Not included yet
No PR review automation, no merge-on-approval, no status-check polling, no stacked PRs, no
automatic branch deletion after merge.

---

## Phase 9 — Manifest-driven cleanup and orphan reporting

### Goal
Destroy is driven by the manifest and validated by ownership labels, and startup reports `sbx-*`
resources that no manifest claims. Nothing is deleted automatically.

### Why this phase comes here
Needs Phase 2 (ownership labels), Phase 5 (tombstones, converging destroy), and Phases 4/6 (the
full set of resources a sandbox owns).

### Current behavior
* Cleanup is name-driven: `remove_project` (`projects/service.py:312`) takes a **project name**,
  resolves it through `inspect_registered_project`, and sweeps by label. A repeat returns **404**
  (`:279`).
* Sibling protection already exists: project-scoped volumes, networks, and containers are only
  swept when no sibling sandbox shares the `project_id` (`:321-326, :353, :372-373, :388`).
* Startup reconciliation (`controller/lifecycle.py:114-244`) marks missing sandboxes, back-fills
  discovered ones (`:177`), emits `controller.unexpected_resource` for orphan containers, and
  expires previews. It audits and marks; **it never recreates and never deletes**.

### Changes
1. Destroy enumerates resources **from the manifest**, not by scanning names: workspace volume,
   agent container, network, database, SQLite data volume, preview leftovers, publication record.
2. Every removal validates ownership labels first. A resource whose name matches but whose
   `orchestrator.sandbox.id` label does not is **left alone and reported**, never removed.
3. Shared infrastructure is explicitly excluded from every sweep: the project mirror volume, the
   shared database server and its data/credentials/network, and lockfile-keyed dependency volumes.
   Extend the existing sibling check to cover the new shared resources.
4. Tombstones (Phase 5) make repeated destroy return the destroyed result.
5. Startup orphan **reporting** for `sbx-*` resources with no manifest row: emit
   `controller.unexpected_resource` events and expose them on a read-only route. **No reaper, no
   automatic deletion, no continuous reconciliation.**
6. Add an explicit operator-triggered cleanup for a reported orphan, so the report has an action.
7. Frontend: complete the migration from `project_name` routes to `sandbox_id` routes, keeping the
   name routes as temporary compatibility that resolves display names.

### Data/schema changes
No new DDL. Uses the Phase 5 tombstone table and the Phase 2 ownership labels.

### API/model changes
* `GET /sandboxes/orphans` — reported, unclaimed `sbx-*` resources.
* `POST /sandboxes/orphans/{resource}/remove` — explicit operator action.
* `DELETE /sandboxes/{id}` returns the tombstone on repeat.
* Name-based project routes are marked deprecated in the schema but still functional.

### Runtime/container changes
None new. This phase removes resources rather than creating them.

### Writer/admission behavior
Destroy is a lifecycle mutation with the draining rule from Phase 5. Orphan reporting is
**read-only** and takes no lock — it must not block anything at startup, and a Docker failure
must degrade to partial counts as reconciliation already does (`lifecycle.py:239-240`).

### Failure and recovery behavior
* **Crash mid-destroy:** `desired_state='destroyed'` persists; a repeat resumes draining. Destroy is
  idempotent.
* **A resource removal fails:** record it in `last_error`, keep the sandbox in `destroying`, and let
  the operation be retried. Do not write the tombstone until the sweep completes, or an
  unremovable resource becomes invisible.
* **Orphan reporting failure:** logged, non-fatal, never blocks startup.

### Security boundary
* Ownership labels are the authority for deletion. This is what prevents destroying another
  sandbox's or another project's resources.
* No credentials are involved.
* Orphan removal is operator-triggered, so no automatic process can delete data.

### Backward compatibility
* Legacy sandboxes may be destroyed and now get a tombstone, so repeated destroy returns the
  destroyed result rather than 404.
* Legacy resources without `sbx-*` names are reported under their existing labels, not swept.
* Name routes keep working.

### Tests
* New: destroy removes exactly the manifest's resources and **preserves the mirror, the shared
  database server, and dependency volumes** — extend the existing sibling tests.
* New: a resource with a matching name and wrong ownership labels is not removed and is reported.
* New: repeated destroy returns the tombstone.
* New: crash mid-destroy resumes and completes on retry.
* New: orphan reporting lists an unclaimed `sbx-*` volume and does not delete it.
* New: destroying one sandbox does not affect a sibling sandbox of the same project.
* `tests/controller/test_lifecycle.py` — extend for orphan reporting; assert startup still
  degrades gracefully when Docker is unavailable.

### Completion criteria
* Destroy is driven by the manifest and validated by labels.
* Shared infrastructure survives individual sandbox destruction.
* A repeated destroy returns the destroyed result instead of 404.
* Unclaimed `sbx-*` resources are reported at startup and never deleted automatically.
* Destroying one sandbox leaves its siblings intact.

### Not included yet
No reaper, no continuous reconciliation, no template garbage collection, no automatic orphan
deletion, no removal of the legacy name routes.

---

# 4. Suggested PR sequence

Each unit should leave the repository valid and testable. Grouped around coherent behavior, not
split into micro-tasks.

| # | Purpose | Files / modules | Depends on | Behavior change? | Tests required |
|---|---|---|---|---|---|
| **1** | Shared Docker test double in `conftest.py`; keep existing stubs working | `tests/conftest.py`, one donor stub | — | preparatory only | existing suite passes unchanged |
| **2** | Migration runner + version-18 rule; no new columns | `controller/store.py` | 1 | preparatory only | double-`initialize` idempotency; version list; upgrade the real DB and the 1..17 backup |
| **3** | `sandboxes` additive columns + legacy backfill (migrations 18–19) | `controller/store.py` | 2 | preparatory only | schema-shape test updated; legacy rows backfilled |
| **4** | **`projects` table rebuild** (migration 20) — highest risk, own PR | `controller/store.py` | 3 | schema change | ID preservation against a real DB copy; `foreign_key_check`; rollback on injected failure; upsert conflict-target still resolves |
| **5** | Naming + manifest modules; deterministic identity | `app/sandboxes/naming.py`, `manifest.py` | 4 | preparatory only | `.hex` guard; name stability; label-ownership refusal |
| **6** | Remote normalization, v1 project store path, `feature_key` validation | `app/projects/`, `controller/store.py` | 5 | behavior (new path only) | URL-form equivalence; userinfo stripping; legacy path cannot reassign a v1 ID |
| **7** | v1 create/read routes + ID routes alongside name routes; frontend `feature_key` field | `app/sandboxes/router.py`, `frontend/src/api` | 6 | behavior | idempotent create; planning attaches without creating a sandbox |
| **8** | **Consolidate git execution** into `app/sandboxes/git.py` | `sandboxes/git.py`, `tasks/service.py`, `delegation/delivery.py`, `projects/service.py`, `previews/service.py` | 1 (independent of 2–7) | behavior-preserving + hardening | all existing git tests pass; hardening asserted; hook script never runs |
| **9** | Project mirror + canonical fetch with read credentials | `app/projects/`, `sandboxes/git.py` | 7, 8 | behavior | fetch container mounts only the mirror; no credential leaks into rows or events |
| **10** | `--no-local` clone, remote/hook stripping, both base pins, linked-worktree refusal | `app/sandboxes/lifecycle.py`, `projects/service.py` | 9 | behavior | no shared inodes; no host remote; both pins recorded; named worktree refusal |
| **11** | **5a** — writer rows before sandbox work; task `preparing`; preview row before Docker | `tasks/service.py`, `agents/service.py`, `previews/service.py`, `delegation/service.py`, `controller/store.py` | 10 | behavior | ordering asserted per writer; `preparing` sentinel; failure paths free the sandbox |
| **12** | **Main-agent writer sessions** (Decision 3) | `agents/`, `controller/store.py`, `turns/` | 11 | behavior | idle does not block; active does; stale reclaim |
| **13** | **5b** — lifecycle lease + crash reclaim | `sandboxes/lifecycle.py`, `controller/store.py`, `controller/lifecycle.py` | 12 | behavior | atomic admission; concurrency; stale reclaim; nesting still works |
| **14** | **5c/5d** — mirror lock, converging create/resume/destroy, draining, tombstones | `sandboxes/lifecycle.py`, `projects/service.py` | 13 | behavior | repeated create/destroy; drain; shared infra preserved; crash resume |
| **15** | **6a** — engine protocol; extract MySQL unchanged; engine-keyed shared names | `app/sandboxes/database.py`, `previews/service.py` | 14 | behavior-preserving | existing shared-database tests pass; legacy names still resolve |
| **16** | **6b** — detection ladder, detection record, `awaiting_engine_confirmation`, `confirm-engine` | `previews/detection.py`, `sandboxes/database.py`, `controller/store.py` | 15 | behavior | conflicting signals never guess; state is not terminal; lease released across the human wait |
| **17** | **6c** — sandbox-owned database, SQLite data volume, previews consume it, `reset-db` | `sandboxes/database.py`, `previews/service.py` | 16 | behavior | migration isolation asserted; `database_failed`; reset idempotency; tracked-DB refusal |
| **18** | Staleness inspection with fetch and freshness reporting | `sandboxes/lifecycle.py`, `projects/` | 17 | behavior | count correctness; degraded answer labelled |
| **19** | **Sync** — safety ref, `pending_base_commit`, rebase/merge rule, `database_failed` | `sandboxes/lifecycle.py` | 18 | behavior | Git restore; migration failure; full `reset-db` recovery; idle agent does not block |
| **20** | Publish part one — push via the mirror with write credentials | `app/sandboxes/publish.py` | 19 | behavior | no credential leaks; sandbox still has no remote |
| **21** | Publish part two — PR discovery, creation, publication record, retry | `app/sandboxes/publish.py`, `controller/store.py` | 20 | behavior | second publish reuses branch and PR; partial-failure retry |
| **22** | Manifest-driven destroy sweep + startup orphan reporting | `sandboxes/lifecycle.py`, `controller/lifecycle.py` | 21 | behavior | label validation; shared infra preserved; orphans reported not deleted |
| **23** | Frontend route migration to `sandbox_id`, name routes deprecated | `frontend/src` | 22 | behavior | UI resolves both |

PRs 1–4 are the riskiest and the least interesting; keep them small and separate. PR 8 is
independent and can land any time after PR 1 — a good parallel track. PRs 11–14 are the
conceptual core and should not be merged into one.

---

# 5. Cross-phase test matrix

| Guarantee | Enforced in | Proving test |
|---|---|---|
| Controller DB migration is ordered and idempotent | 1 | double-`initialize`; `applied_versions() == [1,18,19,20]` |
| Existing project and sandbox IDs survive the rebuild | 1 | upgrade a copy of the live DB; assert IDs byte-identical |
| Pre-squash databases are not corrupted | 1 | upgrade the 1..17 backup; 2..17 never re-run |
| Deterministic sandbox identity | 2 | same `(project_id, feature_key)` → same `sandbox_id`; repeat create returns the same sandbox |
| Credentials never persisted from remote URLs | 2 | tokenized remote absent from rows and events |
| Planning happens after creation and creates nothing | 2 | planning session attaches to an existing sandbox; no sandbox created |
| One git executor; no hook execution | 3 | single implementation; hostile `pre-commit` never runs |
| Git isolation between sandboxes | 4 | `--no-local`; no shared inodes; no host remote in the clone |
| Sandbox never talks to GitHub | 4, 8 | clone has no host-bearing remote, before and after publish |
| GitHub credentials never reach agents or workers | 4, 8 | credential volume, workspace, labels, events all clean |
| Writer rows precede sandbox mutation | 5a | per-writer ordering assertions (task, agent, preview, delegation) |
| **Main-agent idle vs active writer** | 5a/5b | idle container does not block `sync`; active writer session does |
| Main-agent persistence and replacement | 5 | replace the container; workspace, branch, HEAD, manifest, DB unchanged |
| Delegated worker lifecycle | 5 | no container with the task label survives run settlement |
| Lifecycle admission is atomic | 5b | concurrent lifecycle starts yield exactly one winner |
| Nested delegation → task → preview | 5b | full nested flow completes without deadlock |
| Stale lease recovery | 5b | settled-operation lease reclaimed at startup; sandbox usable |
| Crash recovery (partial create) | 5d | resume completes or reports `degraded`, never guesses |
| Destroy idempotency | 5d, 9 | repeated destroy returns the tombstone |
| Shared infrastructure survives destroy | 5d, 9 | mirror, shared DB server, dependency volumes intact; sibling unaffected |
| Database isolation per sandbox | 6 | sandbox DB survives preview start/stop; task preview reaches the same DB |
| **Project-owned migration command isolation** | 6 | runner container spec: mounts, env, network, no GitHub/root credentials |
| Controller never executes project commands | 6 | no host execution path for the command snapshot |
| Engine detection proposes, humans confirm | 6 | conflicting signals → `awaiting_engine_confirmation`; `confirm-engine` exits it |
| `reset-db` rebuilds and converges | 6 | run twice; refuses while a preview is active |
| Sync happy path | 7 | base commit advances; pending cleared; `ready` |
| Git-only sync failure is reversible | 7 | safety ref restores; `current_base_commit` unchanged |
| Failed sync recovery | 6+7 | migration failure → `database_failed`; `reset-db` finalizes `pending_base_commit` |
| Sync is never automatic | 7 | no code path triggers sync on a mirror update |
| Publish retry / partial failure | 8 | push-then-PR-failure retried creates exactly one PR |
| Publish idempotency | 8 | second publish reuses the branch and the PR |
| Legacy compatibility | every phase | legacy create, task settlement, local delivery unchanged; `sync`/`reset-db` refused |
| Legacy never silently becomes v1 | 2, 5, 6, 7 | `lifecycle_version` gates every v1 operation |
| Orphan detection | 9 | unclaimed `sbx-*` reported, never deleted |

---

# 6. Deferred work

Explicitly postponed. Do not pull any of these into v1.

**From the proposal's own scope cuts (9, 10):**
* Blue-green sync workspaces and database swaps — the seam is kept (safety ref, clean-workspace
  requirement) but the parallel-environment machinery is not built.
* Database templates and template garbage collection, including PostgreSQL
  `CREATE DATABASE … TEMPLATE`. Deferred until `reset-db` latency is measured.
* Git object sharing between sandboxes. `--no-local` copies objects; measure clone time and
  storage before optimizing.
* Shared installed dependency trees. A package *download* cache is safer than shared
  `node_modules`; any installed-dependency cache key would need lockfile digest, runtime image
  digest, CPU platform, package-manager version, and its configuration.
* Continuous reconciliation, heartbeat reapers, autonomous repair loops.
* Multi-host scheduling and distributed locks. The backend remains single-worker.
* Stacked sandboxes and stacked feature branches.
* Automatic sync on every `main` update.
* A generic saga or resource-graph framework.
* `shared_data` for new engines — it stays MySQL-and-legacy-only.

**Carried over as known but unaddressed:**
* Dependency volumes are never removed and accumulate one per distinct lockfile per sandbox.
  Count them before adding any new volume class.
* ADR 0003 (dependency volumes are `ro`) and ADR 0005 (remotes and hooks are stripped) are both
  contradicted by current code. Phase 3 and Phase 4 make ADR 0005 true **for v1 clones only**;
  ADR 0003's overstatement is not corrected by this plan. Correcting or superseding both ADRs is
  separate work.
* Legacy sandboxes are never converted. An explicit recreate operation is the only path to v1, and
  building a guided recreate flow is not in this plan.
* PR review automation, merge-on-approval, and status-check polling.
* Removing the legacy name-based routes.

---

# 7. Definition of v1 complete

The target flow works end to end:

```
human selects a Git-backed project
        ↓
human supplies feature_key
        ↓
sandbox created from a known origin/main commit
        ↓
persistent main agent available
        ↓
main agent inspects the project
        ↓
planning occurs inside the sandbox
        ↓
plan finalized
        ↓
work delegated
        ↓
short-lived workers perform tasks
        ↓
workers disappear after completion
        ↓
preview/test against the sandbox-owned database
        ↓
explicit sync when needed
        ↓
reset-db if database recovery is required
        ↓
controller publishes a reviewed branch
        ↓
controller creates or reuses a PR
        ↓
sandbox can be destroyed safely
```

Observable checklist:

- [ ] Upgrading a copy of the existing controller database succeeds; project and sandbox IDs are unchanged.
- [ ] Running migrations twice produces the same schema and version list; pre-squash databases apply only 18+.
- [ ] Repeated v1 creation with the same project and `feature_key` resolves to the same sandbox.
- [ ] A v1 sandbox is an independent `--no-local` clone with no GitHub remote and no hooks, pinned to a recorded `origin/main` commit.
- [ ] `created_base_commit` never changes; `current_base_commit` moves only on a successful sync.
- [ ] Planning starts after sandbox creation, inspects that sandbox, and never creates a second one.
- [ ] Replacing the main-agent container loses no workspace, Git, database, or manifest state.
- [ ] An idle main-agent environment does not block `sync`; an active main-agent write turn does.
- [ ] Delegated workers disappear after task completion; no worker pool exists.
- [ ] Delegation → task → preview still nests without deadlock.
- [ ] A preview cannot start while a lifecycle lease excludes new writers.
- [ ] A killed backend holding a lease or writer session recovers on restart.
- [ ] One database belongs to the sandbox, not to a preview; SQLite lives outside the Git workspace.
- [ ] Engine detection proposes; conflicting signals stop at `awaiting_engine_confirmation`; `confirm-engine` exits it.
- [ ] Project migration commands execute inside the sandbox runtime with sandbox-scoped DB credentials, no egress, and no controller or GitHub credentials.
- [ ] A failed database migration during sync enters `database_failed`; `reset-db` rebuilds and finalizes `pending_base_commit`.
- [ ] Git-only sync failure restores from the safety ref; sync never runs automatically.
- [ ] A second publish reuses the existing remote branch and PR; a push-then-PR failure retries to exactly one PR.
- [ ] No agent or worker ever receives a GitHub credential; the sandbox clone still has no remote after publishing.
- [ ] A repeated destroy returns the destroyed result instead of 404.
- [ ] Destroy preserves the project mirror, the shared database server, and dependency volumes; siblings are unaffected.
- [ ] Unclaimed `sbx-*` resources are reported at startup and never deleted automatically.
- [ ] Every legacy sandbox retains inspection, task settlement, and local delivery; `sync` and `reset-db` are refused; nothing is silently reinterpreted as v1.
