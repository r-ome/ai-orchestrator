# Architecture Review — Verification and Work Plan

**Source reviewed:** `docs/plans/ai-orchestrator-architecture-review-consolidated.md`
**Verified against:** `main` @ `30e7ca9`, 19 Aug 2026
**Baseline at verification:** `backend/.venv/bin/python -m pytest -q` → 818 passed, 43 skipped, 26.1s

> **Progress, 19 Aug 2026 — `main` @ `ce205cd`.** Phases 0, 1, 2, 3.1, 3.2, 3.3, 4 and 5 are
> done. Phase 3.4 is partially done and deliberately stopped; see its status block. Phase 6 is
> next. Current suite: **822 passed, 43 skipped**.
>
> The gated suite (`RUN_DOCKER_PREVIEW_TESTS=1`) is **5 failed, 855 passed**. All five are
> genuine test rot, unrelated to any phase:
> `tests/previews/test_docker_integration.py::test_approved_proposal_starts_and_stops_through_the_full_service`,
> `::test_events_websocket_replays_and_streams_live_container_logs`,
> `tests/previews/test_preview_kinds.py::test_task_preview_serves_its_commit_and_keeps_it_across_a_restart`,
> `::test_failed_task_preview_returns_the_task_to_review` — all four build a preview without
> registering its project, which a later phase made mandatory — and
> `tests/tasks/test_docker_tasks.py::test_an_agent_written_preview_proposal_cannot_move_the_task`,
> which asserts `.agent` appears in a refusal detail that now reads
> "Task branch '...' has no commit beyond ...".
>
> Gated runs need a live daemon and leak roughly 2 containers, 5 networks and 30 volumes each.
> Leaked `orchestrator-preview-*` networks are not harmless: 28 of them exhausted Colima's
> address pool and broke 5 unrelated tests until pruned.
>
> **The figures in the phase text below are the review's, not the tree's.** Each phase that has
> run carries a status block with corrections. Where they disagree, the status block wins.

## Verdict

The consolidated review is accurate. Every structural claim checked out against the
tree. This is unusual for review documents in this repo, so the figures below record
what the code actually shows, including the four places the review's numbers are wrong.

Nothing in the review argues for a rewrite, and nothing found here changes that.

---

## 1. Verification results

### Confirmed exactly

| Claim | Review says | Measured |
|---|---|---|
| Package import cycle | 9-package SCC | 9-package SCC: `agents, controller, delegation, implementation_context, planning, previews, projects, sandboxes, tasks` |
| Packages outside the cycle | `containers`, `volumes`, `turns` | same; `turns` imports in, nothing imports it back |
| Function-local imports breaking cycles | 13 | 13 |
| Cross-package private import | `sandboxes/lifecycle.py` ← `tasks.service._stop_task_preview` | `app/sandboxes/lifecycle.py:179` |
| `controller/store.py` | ~4,511 lines, 155 methods, ~30 tables | 4,511 lines, 155 methods, 33 `CREATE TABLE` |
| `previews/service.py` | ~3,709 lines, ~100 functions | 3,709 lines, 104 top-level functions |
| `sandboxes/router.py` | ~1,255 lines, ~50 `HTTPException` | 1,255 lines, 49 `HTTPException` |
| Byte-identical router/service helpers | 4 | 4: `_sync_strategy`, `_base_branch`, `_json_value`, `_optional_string` — verified identical by diff |
| Same rule, different exception type | 1 | `_require_v1` raises `HTTPException`; `require_v1` raises `SandboxNotFound`/`SandboxConflict` |
| Substring control flow | missing-workspace recovery | `app/sandboxes/router.py:851` — `if "workspace is missing" not in str(error)` |
| Legacy fixture dominance | ~30 legacy vs ~7 v1 | 30 test files use `register_sandbox`, 7 use `register_v1_sandbox` |
| Legacy path unreachable | production returns before legacy branch | `projects/service.py:110` returns early; all 3 callers of `ensure_sandbox_registered` pass no `project=` |
| Two shared-database implementations | previews vs sandboxes | `previews/service.py:1770 _shared_database_server` (MySQL, under `_shared_database_lock`) vs `sandboxes/database.py:1381 _ensure_shared_server` (engine-generic, **no lock**) |
| `_object` helper drift | one copy catches `TypeError` | 4 copies; only `delegation/change_requests.py:560` catches `(TypeError, ValueError)` |
| Repeated `_progress` helpers | 5 | 5, in `service`, `execution`, `driver`, `change_requests`, `integration_review` |
| Repeated `_integer` helpers | listed | 7 |
| Stringly Docker collection lookup | `getattr(client, f"{kind}s")` | 2 sites: `sandboxes/router.py:481`, `:1079` |
| Docker labels owned by feature packages | previews + agents define them | 13 `LABEL_*` in `previews/service.py`; `tasks/runner.py`, `planning/runner.py`, `turns/locators.py`, `controller/lifecycle.py` all import labels **from previews** |
| Agents import private preview helpers | yes | `agents/service.py:33` imports `_dependency_volume`, `_lockfile_digest`, `_volume_runtime_files` |
| Preview config owns shared settings | yes | `PreviewSettings.git_image` is imported by `sandboxes`, `tasks`, `agents`, `delegation` |
| `planning/runner.py` is generic | yes | imported by 6 non-planning modules for `extract_payload`, `run_validated_turn`, `TurnRequest`, labels |
| Log streaming behind a preview name | yes | `turns/router.py:26` imports `open_preview_log_stream` |
| "Project" means a sandbox | yes | `/projects/{project_name}/planning`; `project_name` is passed straight to sandbox resolution |
| Frontend hotspots | ~1,780 / >1,200 lines | `DelegationWorkspace.tsx` 1,782; `PlanningSessionPage.tsx` 1,268 |
| Frontend test gap | zero test files | 0 `*.test.*` / `*.spec.*` under `frontend/src` |
| Hardened boundary is complete | all execution routes through it | 0 direct `containers.run(` / `containers.create(` outside `containers/hardened.py` |

### Corrections to the review's figures

1. **`operation_phase` write sites: 25, not 8.** Twelve in `sandboxes/service.py`, twelve
   in `sandboxes/router.py`, one in a `store.py` `UPDATE`. The important half of the claim
   holds and is stronger than stated: **0 reads in application code, 0 in response models,
   0 in `frontend/src`.** Distinct values written: 18. Only 4 test assertions read the
   attribute directly (11 mentions total in tests).
2. **Identical `OperationError` classes: 10, not 14.** `Task`, `Project`, `Planning`,
   `Agent`, `Context`, `Delegation`, `Verification`, `Preview`, `Volume`, `Container` —
   all byte-identical `(status_code, detail)` bodies. The competing sandbox family
   (`SandboxConflict`, `SandboxNotFound`, `SandboxInternalFailure`) is real and separate.
3. **Route error mapping is already mostly unified.** A shared `docker_response(fn, policy)`
   exists and 8 of 11 routers use it through a two-line wrapper. Only three routers stand
   outside: `sandboxes/router.py` (49 inline `HTTPException`), `projects/router.py` (5),
   `turns/router.py` (0 — nothing to map). The review frames this as a broad
   inconsistency; it is really one router.
4. **`store.py` has 33 tables, not ~30**, and `previews/service.py` has 104 top-level
   functions, not ~100. Minor, but the concentration is slightly worse than described.
5. **`SandboxManifest.operation` is write-only too**, not just `operation_phase`. Zero
   application readers; 2 test assertions. Both fields go in Phase 4.
6. **The frontend has no test tooling at all** — not merely no test files. `package.json`
   has no vitest, jest, testing-library, or DOM environment. Phase 0.3 is an install and
   configure task, not just a "write tests" task.

### Round 2 — the shared-database race, proven

The review flagged this as "likely, not reproduced." It is now **proven, and it is worse
than the review describes.**

**The lost update.** `_read_or_create_server_credentials`
(`sandboxes/database.py:817`) reads the credentials file, and if it is absent, generates
and writes new credentials. Read and write are separate Docker commands with no lock and
no atomic create. A probe driving the real function from two threads (only the Docker
side-effect helper faked) produced two callers holding different root passwords for one
shared server, one of which is not the password on the volume.

Consequence: the server bakes the winner's root password in at `initdb`, which runs once
on an empty data directory. The loser's credentials can end up persisted on the volume.
That is **permanent corruption of a project's shared database credentials**, not a
transient failure.

**The container-creation race is NOT the bug.** Both MySQL and PostgreSQL `provision`
already catch `APIError` and re-fetch when `shared=True`, with a comment naming the case.
Any fix should target the credentials path, not container creation.

**The paths collide on identical resources.** The review implies previews and sandboxes
are parallel implementations for different sandbox shapes. They are not:

- `previews._shared_database_names(key)` **is** `mysql_shared_database_names(key)` — the
  same function `sandboxes.shared_database_names(key, "mysql")` calls.
- `previews._project_key(project)` is `managed_project_key(source_path)`, which strips the
  `managed:` prefix to yield exactly `sandbox["project_id"]` — the key the sandboxes path
  uses.

So for a MySQL project both paths resolve **byte-identical** container, data volume,
credentials volume, and network names. Only the previews side takes
`_shared_database_lock`. A lock one of two contenders takes is not mutual exclusion.

**It is reachable on v1, not just legacy.** `sandbox_database_runtime` returns `None` when
`db_engine == NO_DATABASE`. A **v1** sandbox with no database, running a preview that
requests a shared database, falls through to `_attach_shared_database`. Meanwhile a
sibling v1 sandbox in the same project with `engine == mysql` runs `_ensure_shared_server`
unlocked. `previews/service.py` contains **zero** references to `lifecycle_version` — it
never gated on the sandbox shape at all.

**No admission control is per-project.** Every partial unique index is `per_sandbox`,
`per_delegation`, or `per_session`. `sandbox_leases` is keyed by sandbox id. `confirm-engine`
holds only `lifecycle_lease(store, sandbox_id, ...)`. Nothing serialises two sandboxes of
one project.

**Missing image-mismatch check.** `previews/service.py:1826` refuses to adopt a shared
server whose `orchestrator.shared-database.image` label differs from the requested image.
`sandboxes/database.py` writes that label (line 1396) and never compares it. A sandbox can
silently adopt a server running a different engine image.

*Probe kept out of the repo; it is a throwaway, and the real regression test belongs in
Phase 3.*

### Still not verified

- Runtime behaviour of preview execution modes, Docker reconciliation, and publish. The
  suite passes but exercises these with fakes.
- Whether the credentials corruption has occurred in practice on this machine.

### Interaction with the earlier 10-candidate review

The prior review's candidates 1–9 are merged. Two of its results already show here:
the shared `docker_response` policy (candidate 8) and `useApiResource` polling
(candidate 9). Its remaining candidate 10 — one `Severity` enum for planning findings
and integration review — does **not** exist in the tree yet (`grep 'class Severity'`
returns nothing) and is folded in below as an optional item.

---

## 2. Sequencing principle

Two rules govern the order:

1. **Subtract before you move.** Deleting the legacy sandbox shape removes the duplicate
   shared-database implementation as a side effect. Moving it first would move code that
   is about to be deleted.
2. **Never mix a file move with a behaviour change** in one commit. The store split and
   the layer directories are large mechanical diffs; they must be reviewable by reading
   the file list, not the hunks.

Every phase must leave `pytest -q` green and the repo shippable.

---

## 3. Work plan

### Phase 0 — Guardrails (prerequisite for everything)

**0.1 Import-direction test.** Add a test that computes the package SCC and asserts it
does not grow. Start by pinning the current 9-package component as a known-bad baseline,
then shrink the allowance as phases land. This is the single cheapest thing in the plan
and it stops the cycle getting worse while the rest proceeds.

**0.2 Missing-workspace recovery test.** One focused regression around
`sandboxes/router.py:851` before touching that path.

**0.3 Frontend test harness.** Confirmed absent: `frontend/package.json` declares no test
runner, no `@testing-library/*`, and no DOM environment. Scripts are `dev`, `build`,
`lint` (oxlint), `preview` only. This phase must add vitest + `@testing-library/react` +
jsdom (or happy-dom), a `test` script, and wire it into whatever gate runs `lint`/`build`.
Phase 6 cannot start without it.

*Risk: none. Adds only tests.*

---

### Phase 1 — Cheap deletions and one-home moves

**1.1 Move Docker label constants to a neutral module.** Create `app/labels.py` (or
`app/containers/labels.py`) and move the 13 `LABEL_*` names out of `previews/service.py`.
Fixes 4 wrong-owner imports (`tasks/runner`, `planning/runner`, `turns/locators`,
`controller/lifecycle`). Pure rename.

**1.2 Collapse the 10 identical `OperationError` classes** into one shared
`OperationError` in a neutral module. Keep the name-per-domain as aliases if the routers
depend on catching them separately; only keep a real subclass where handling differs.

**1.3 Delete the 4 duplicated sandbox helpers.** Keep the `service.py` copies; the router
imports them.

**1.4 Consolidate the drifted helpers.** One `_object` (keeping the widest correct
behaviour — the `(TypeError, ValueError)` version), one `_integer`, one `_progress`
keyed by the already-central event-kind vocabulary.

**1.5 Replace the two `getattr(client, f"{kind}s")` sites** with an explicit dict.

*Risk: low. All mechanical, all covered by the existing suite.*

---

### Phase 2 — Typed exception at the workspace boundary

Introduce `WorkspaceMissing` at the mirror/workspace boundary and delete the substring
match in `sandboxes/router.py:851`. Requires 0.2 first.

*Risk: low. One control-flow path.*

---

### Phase 3 — Retire the legacy sandbox shape (largest deletion)

**3.1 Convert 30 test files** from `register_sandbox` to a shared `register_v1_sandbox`
fixture. Dedicated mechanical commit, no production changes. Noisy but low-risk.

**3.2 Delete the legacy branches:** `store.register_sandbox`, the backstop registration
in `projects/service.py:114`, legacy baseline coverage, and lifecycle-version guards that
no longer protect a reachable path.

**3.3 Fix the credentials race — do this FIRST, and independently.** Round 2 upgraded
this from cleanup to a live bug on v1 sandboxes (see above). It should ship as its own
commit before any deletion, because it is reachable today:

- make `_read_or_create_server_credentials` atomic, or serialise the whole get-or-create
  on a per-project key that **both** call sites take;
- port the image-mismatch check from `previews/service.py:1826` into `_ensure_shared_server`;
- add a regression test driving both entry points concurrently.

A process-local lock is sufficient — this is a single-process local control plane — but it
must be taken by both paths, not one.

**3.4 Then converge the two shared-database implementations.** They already resolve
byte-identical Docker resource names, so this is convergence onto one implementation, not
deletion of a dead fallback as the review assumed. The preview path stays reachable for v1
sandboxes with `db_engine == NO_DATABASE`, so it cannot simply be removed with the legacy
shape — that gap has to be handled deliberately.

*Risk: 3.3 low and urgent. 3.4 medium and larger than the review estimated.*

**3.4 status, 19 Aug 2026: partially done, delegation deliberately not attempted.**

A later handoff scoped 3.4 as "four differences to thread" between
`previews._shared_database_server` and `sandboxes.database._ensure_shared_server`. Reading both
functions found twelve. What the earlier scoping got right: the eight container labels are
byte-identical, `shared_database_names(key, "mysql")` returns exactly
`mysql_shared_database_names(key)` so both paths address the same container, and
`DATABASE_ENGINES["mysql"] is MYSQL_DATABASE`.

What it missed. Beyond the known four (the `report` ProgressReporter, image source, error
factory, return shape) and the known volume-label divergence:

| Difference | Behavioural |
|---|---|
| Image-mismatch 409 fires before provisioning in sandboxes, after it in previews | yes |
| Health check: previews raises `PreviewOperationError`; sandboxes raises 422/408 with its own wording | yes |
| Image pull failure: previews maps to 424 "Preview image '...' is unavailable"; sandboxes lets the raw `DockerException` escape | yes |
| `database=` argument: previews passes the proposal's database name, sandboxes passes `""` | yes |
| Container start: previews starts a newly created container unconditionally, sandboxes starts only when not running | yes |
| Network lookup: `networks.list(names=[...])` vs `networks.get(...)` | no |
| Engine dispatch: module constant vs `database_engine(name, error)` | no |

Making previews delegate would change the error codes and progress events the preview UI shows,
so it is not a refactor. Three routes remain open: accept the drift, thread all twelve
differences as parameters (which makes the callee a twelve-parameter function, arguably worse
than the duplication), or leave the two implementations separate.

Done instead: `sandboxes/database.py` now uses the `app/labels.py` constants rather than
hardcoding the same fifteen label keys. Zero behaviour change, and it removes the reason a
reader would think the two label sets could drift.

Still hardcoded elsewhere: 26 more label-key literals across eight files —
`sandboxes/naming.py`, `sandboxes/engine_detection.py`, `tasks/runner.py`, `planning/runner.py`,
`agents/service.py`, `implementation_context/inventory.py`, `delegation/verification.py` and
`controller/lifecycle.py`. Out of scope here; a candidate for Phase 9.

---

### Phase 4 — Delete write-only manifest state

Remove both `SandboxManifest.operation_phase` and `SandboxManifest.operation`. Confirmed:
`operation_phase` has 25 write sites, 18 distinct values, 0 application readers, 4 test
assertions; `operation` has 0 application readers and 2 test assertions. Delete the 6 test
assertions that keep them alive; progress detail already lives in the event stream.

Do **not** confuse this with the `operation` column on the **lease** table, which is read
at `sandboxes/lifecycle.py:57` and stays.

Sequenced after Phase 3 and before Phase 5 deliberately: those 25 write sites are threaded
through the exact handlers Phase 5 moves.

*Risk: low.*

---

### Phase 5 — Finish the sandbox service migration

Move `create_or_resolve_sandbox`, `resume_sandbox`, `delete_sandbox`, `confirm_engine`,
and `reset_database` bodies out of `sandboxes/router.py` into `sandboxes/service.py`.
Move behaviour first with no simplification; clean up in a second commit once green.

Then route `sandboxes/router.py` and `projects/router.py` through the existing
`docker_response(fn, policy)` convention, retiring the 54 inline `HTTPException`s.

The router should end as: validate request → one service call → error mapping.

*Risk: medium — dense lock-ordering and lifecycle sequencing. Highest value per unit of
risk in the plan, and by then Phases 3 and 4 have already removed much of the bulk.*

**Phase 5 status, 19 Aug 2026: done.** Three commits, `a4e5cdd`, `15d32b4` and `ce205cd`.

`sandboxes/router.py` 1220 → 614 lines; `sandboxes/service.py` 695 → 1487. All 14 sandbox
handlers now read as validate request → one service call → build the response; the largest is
31 lines and is almost entirely argument mapping. No handler reaches into a `service._private`
function.

Corrections to the text above:

- **55 inline `HTTPException`s, not 54** — 50 in `sandboxes/router.py`, 5 in `projects/router.py`.
  49 are retired. The 7 that remain are deliberate: two 400 request-shape guards, and
  `remove_orphan_resource`'s own `NotFound` and `DockerException` mappings, which carry details
  the shared policy would overwrite.
- **Five handler bodies were not enough to reach the target shape.** Two more held domain logic
  the plan never named: `_sandbox_staleness_response` (70 lines: mirror lock, canonical fetch,
  staleness count) and `remove_orphan_resource`. Both moved in `ce205cd`.
- The service exceptions gained `status_code` and `detail` so they fit
  `DockerErrorPolicy.domain_errors`. `SandboxUnavailable` (503) is new, added because the orphan
  path returns Docker's own message and `docker_response` would have replaced it with a constant.

Two coverage gaps this phase exposed, both now closed:

- `projects/router.py` had **no tests at all**. Its new error policy was unverified — deleting
  the policy left the suite green. `tests/projects/test_router.py` covers it in six tests.
- The new `SandboxUnavailable` mapping was likewise unbound until
  `test_orphan_removal_reports_a_docker_outage_as_unavailable` was added.

Verified by: a 38-branch error-path probe identical across each commit, a byte-identical
generated OpenAPI schema, normalised AST diffs of every moved body against its original, and
mutation checks on each new policy. `pytest -q` 815 → 822 passed, 43 skipped.

---

### Phase 6 — Frontend workflow composition

Requires 0.3. Add tests for the "what am I watching?" projection in
`DelegationWorkspace.tsx` (1,782 lines), then split it and `PlanningSessionPage.tsx`
(1,268 lines) around one view-model layer. Where the browser reconstructs server rules,
prefer improving the backend projection over adding frontend logic.

*Risk: medium, currently unprotected by any test.*

**Both line counts re-measured 19 Aug 2026 and correct**: `frontend/src/components/DelegationWorkspace.tsx`
is 1,782 lines and `frontend/src/pages/PlanningSessionPage.tsx` is 1,268. The first plan figures
in this document that needed no correction.

"Unprotected by any test" is literal: the whole frontend has one test file,
`frontend/src/test/smoke.test.tsx`. Build the safety net before the split.

Do not delegate this phase to Codex. It cannot screenshot and localhost is blocked to it.
Claude's own Bash can drive headless CDP against this app.

---

### Phase 7 — Split `ControllerStore`

Split 155 methods across 33 tables into `store/{connection,migrations,projects,sandboxes,
planning,implementation,previews,agents,events}.py`. Measured distribution confirms the
split is balanced and matches the proposed module list: sandboxes 35, implementation 34,
connection/shared 27, planning 22, previews 12, agents 10, tasks 8, projects 5, events 2. Keep one database, one connection
and locking strategy, one migration stream, every partial unique index, and the direct-SQL
style. Expose a facade so `get_controller_store()` keeps working while callers migrate.

**Explicitly not in scope:** schema redesign, ORM, repository pattern.

*Risk: mechanically large, conceptually low. Deliberately after Phase 5 — its benefit
accrues gradually while Phases 3–5 are immediate.*

---

### Phase 8 — Decompose previews

Only after Phase 3. Extract project secrets to the projects domain, extract the dependency
cache into a neutral module (removing the 3 private imports from `agents/service.py`),
then split what remains into `proposal.py`, `lifecycle.py`, `protected_files.py`,
`runtimes/{native,dockerfile,compose}.py`, `network.py`.

Add a typed `PreviewStatus` only for lifecycle that is genuinely read. Do not replace
unused string state with an unused enum.

*Risk: low-to-medium.*

---

### Phase 9 — Layer directories and one dependency direction

Target: `platform → store → domain → api`, imports pointing downward.

Move the neutral mechanics identified above into `platform/`: Docker execution and labels,
provider command construction and stream parsing (out of `planning/runner.py`), dependency
caches, `jobs.py`, `log_stream.py`, errors, `env.py`. Move startup reconciliation
composition out of `controller/`. Move the shared runtime/Git settings out of
`previews/config.py`.

Then tighten the Phase 0.1 test from "does not grow" to "no cycles".

*Prerequisites: Phases 1, 5, 7.*

---

### Phase 10 — Vocabulary (optional, low urgency)

Make the hierarchy explicit in names and docs:

```
ApprovedPlan → ImplementationContext → WorkItem → WorkItemRun
  → SandboxChange (today's Task) → Verification → FeatureReview → Publication
```

Rename the internal task-branch concept toward `SandboxChange`, preserving wire
compatibility. Optionally fold `implementation_context`, `delegation`, and `tasks` under
`domain/implementation/`. Structural move only.

**Also here:** the earlier review's outstanding candidate 10 — one `Severity` enum. Its
premise is partly wrong. There are **three** vocabularies, not two:

| Site | Values | Gate |
|---|---|---|
| planning reviewer findings (`planning/service.py:1032`) | `blocking, major, minor` | blocking/major |
| planning risks (`planning/service.py:990`) | `high, medium, low` | — |
| integration review findings (`delegation/integration_review.py:398`) | `low, medium, high` | high/medium |

Only the last two share a scale. Planning findings use a genuinely different one, and the
frontend renders all three as pill labels, so collapsing them changes the wire format. Scope
this to unifying the two `{high, medium, low}` sites, or drop it.

---

## 4. Explicitly rejected

- A rewrite, or Option C's durable workflow engine. Nothing found justifies it, and it
  would risk the lock ordering, Git-pinned review, protected-file approval invalidation,
  and restart reconciliation that are the repo's best properties.
- Celery, Temporal, an event bus, or a distributed queue.
- Replacing serial implementation execution with concurrency. It is a deliberate
  simplification around one writable workspace, not a defect.
- Combining the store split with a schema or ORM change.

## 5. What must not move

Verified as working and worth defending: the hardened container boundary
(`containers/hardened.py`, 332 lines, 100% of execution call sites); explicit lifecycle
transition tables; partial unique indexes as admission control; Git as code truth with
fast-forward acceptance; approval-gated preview execution; append-only run history;
startup reconciliation.
