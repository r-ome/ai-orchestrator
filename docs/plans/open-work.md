# Open work

**State:** `main` @ `7b497c4`, 19 Aug 2026
**Suites:** backend 835 passed, 43 skipped, ~31s. Frontend 80 passed, `npm run build` clean.

This replaces `architecture-review-verification-and-plan.md` and
`ai-orchestrator-architecture-review-consolidated.md`. Both are deleted. The plan they
described is complete: phases 0 through 10 are resolved, and phase 3.4 is stopped by an
explicit decision, not by oversight. Their per-phase status blocks live in the git history
and in the commit messages that landed each phase.

Every figure below was measured on `7b497c4`. The method for each is given, so the next
reader can re-measure rather than trust.

---

## 1. Phase 3.4 — two shared-database implementations, decision open

The only piece of the old plan that is unfinished. It stopped because it is not a refactor.

The review assumed one implementation was a dead fallback. Both are live:
`previews/sharing.py:133 _shared_database_server` and
`sandboxes/database.py:1393 _ensure_shared_server`. They resolve byte-identical Docker
resource names, so both address the same container, but they behave differently on failure.

A handoff scoped this as "four differences to thread". Reading both functions found twelve,
five of them behavioural:

| Difference | Behavioural |
|---|---|
| Image-mismatch 409 fires before provisioning in sandboxes, after it in previews | yes |
| Health check raises `PreviewOperationError` vs 422/408 with its own wording | yes |
| Image pull failure maps to 424 vs letting the raw `DockerException` escape | yes |
| `database=` gets the proposal's name vs `""` | yes |
| Container start is unconditional vs only-when-not-running | yes |
| `networks.list(names=[…])` vs `networks.get(…)` | no |
| Engine dispatch: module constant vs `database_engine(name, error)` | no |
| The `report` ProgressReporter, image source, error factory, return shape, volume labels | mixed |

Making previews delegate to sandboxes changes the error codes and progress events the
preview UI renders. It cannot land as a structural commit.

**Three routes, none chosen:**

1. Accept the drift and document it.
2. Thread all twelve differences as parameters. This yields a twelve-parameter function,
   arguably worse than the duplication.
3. Leave the two implementations separate, permanently and on purpose.

The preview path also stays reachable for v1 sandboxes with `db_engine == NO_DATABASE`, so
it cannot simply be deleted.

---

## 2. Never covered by any phase

### The two largest backend modules were never decomposition targets

```text
1,648  app/sandboxes/database.py
1,487  app/sandboxes/service.py
1,291  app/planning/service.py
1,259  app/delegation/execution.py
1,097  app/previews/service.py
1,080  app/tasks/service.py
```

Phase 7 split `ControllerStore`; phase 8 split `previews`. Nothing did this. Phase 5 grew
`sandboxes/service.py` from 695 to 1,487 lines by design and recorded it.
`sandboxes/database.py` also holds one of the two implementations in item 1.

For contrast, the frontend hotspots the review named were fixed: `DelegationWorkspace.tsx`
went 1,780 → 525 lines, `PlanningSessionPage.tsx` 1,200+ → 635.

### Runtime behaviour is still only exercised with fakes

Preview execution modes, Docker reconciliation, and publish pass the suite against fakes.
The gated suite that would touch them for real has not run since `ce205cd` — four handoffs.
Docker is up on this machine; `docker.from_env().ping()` succeeded on 19 Aug 2026, so this
is runnable now.

Five failures were known at `ce205cd` and are test rot, unrelated to any phase:

- `tests/previews/test_docker_integration.py::test_approved_proposal_starts_and_stops_through_the_full_service`
- `::test_events_websocket_replays_and_streams_live_container_logs`
- `tests/previews/test_preview_kinds.py::test_task_preview_serves_its_commit_and_keeps_it_across_a_restart`
- `::test_failed_task_preview_returns_the_task_to_review`

The first four build a preview without registering its project, which a later phase made
mandatory. The fifth,
`tests/tasks/test_docker_tasks.py::test_an_agent_written_preview_proposal_cannot_move_the_task`,
asserts `.agent` appears in a refusal detail that now reads
"Task branch '...' has no commit beyond ...".

Gated runs leak roughly 2 containers, 5 networks and 30 volumes each. Leaked
`orchestrator-preview-*` networks are not harmless: 28 of them exhausted Colima's address
pool and broke 5 unrelated tests until pruned. Prune before and after.

---

## 3. Structural debt, measured

### The 8-node domain import cycle

`agents, delegation, implementation_context, planning, previews, projects, sandboxes, tasks`.

Genuine mutual domain coupling. Phase 9 cut it from 10 nodes to 8 by moving three things:
`dependency_cache` into `previews/`, the sandbox status enum into `controller/store/`, and
startup composition out to `app/startup.py`. Every remaining edge runs between domain
packages, so **no further file move can shrink it**. Cutting it needs signature changes,
which means a phase of its own.

`tests/test_import_direction.py` guards it. Note it is a **two-way ratchet**: it fails if
the cycle grows *and* if it shrinks, telling you to tighten `KNOWN_CYCLE`. Nobody can cut an
edge without updating it. Its node set deliberately includes root-level modules — keep that,
even though `main.py` and `startup.py` are the only ones left and it catches nothing extra
today. It exists to stop the next root module from reopening the blind spot that hid a real
cycle for eight phases.

### 20 hardcoded label literals across 8 files

Method: `grep -rhoE '"orchestrator\.[a-z.]+"' app`. 29 occurrences, 17 unique, 20 outside
the canonical `app/platform/labels.py`.

```text
5  app/platform/naming.py
4  app/sandboxes/engine_detection.py
4  app/agents/service.py
2  app/implementation_context/inventory.py
2  app/delegation/verification.py
1  app/tasks/runner.py
1  app/startup.py
1  app/planning/runner.py
```

### 14 function-local imports

Method: AST walk for `Import`/`ImportFrom` nodes inside a function body — not grep.

```text
app/controller/store/projects.py:24    register_v1_project
app/delegation/execution.py:1251       _provider
app/delegation/service.py:415          view
app/planning/service.py:1154           _generated_at
app/sandboxes/lifecycle.py:178,179,191 _stop_blocking_preview
app/sandboxes/lifecycle.py:251,270     drain_sandbox_writers
app/sandboxes/service.py:1206          resume
app/startup.py:92                      _reject_abandoned_tasks
app/tasks/service.py:210               run_task
app/tasks/service.py:750,751           _stop_task_preview
```

Nine sit inside the 8-node cycle and are plausibly load-bearing. The `startup.py:92` one is
the candidate worth testing first: `startup` is acyclic now, so the cycle it plausibly
dodged is cut. Unproven, and hoisting it is a behaviour change.

### `PreviewStatus` was never added

Carried from phase 8. Preview status is still strings. Nothing has established which states
are genuinely read, and that analysis has to come first.

---

## 4. Small, cheap, unblocked

- **Three dead imports** in `tests/delegation/test_delivery.py`: `mkdtemp` (line 6),
  `ControllerStore` (line 14), `IntegrationReviewStatus` (line 21). Each name appears
  exactly once in the file, on its own import line.
- **`tests/planning/test_reconcile.py::test_reconcile_fails_and_releases_running_turn_when_docker_is_down`
  never exercises the premise in its name.** It passes with its Docker patch deleted and a
  live daemon running, because every assertion concerns `_settle_interrupted_turns`, which
  runs before any Docker call. Found by deleting the patch, not by reading it.
- **`tests/controller/test_lifecycle.py` now tests startup composition** and arguably belongs
  at `tests/test_startup.py`.
- **Frontend layering oddity:** `components/PlanningAgentInspector.tsx:3` imports a type from
  `pages/planningSessionModel.ts`. A component reaching into a page.
- **`ruff` is not installed anywhere in this environment.** No lint gate has run on any
  commit for several phases. Either install it or stop asking agents to run it.

---

## 5. Decided and closed — do not reopen without reading this

### One `Severity` enum — closed unbuilt, 19 Aug 2026

The candidate pairs the wrong two sites. Three vocabularies exist, not two:

| Site | Values | Gate |
|---|---|---|
| planning reviewer findings | `blocking, major, minor` | `{blocking, major}` blocks (`planning/service.py:766`) |
| plan risks | `high, medium, low` | none |
| integration review findings | `low, medium, high` | `{high, medium}` blocks (`integration_review.py:501`) |

Plan risks and integration findings share a scale but not a concept — one is advisory, the
other blocks a delegation. Reviewer findings and integration findings share the concept but
not the scale. Unifying that pair is a wire change: the vocabulary is in the DB column
(`controller/store/schema.py:219`), both LLM prompts (`planning/prompts.py:107`,
`integration_review.py:489`), and the frontend.

Done instead: `frontend/src/utils/severity.ts` maps both vocabularies onto shared rungs and
pill colours. Three finding lists had hardcoded `pill warn`, drawing a `blocking` finding in
the same amber as a `minor` one.

### The `SandboxChange` rename — closed unbuilt, 19 Aug 2026

`change` is already spent. `app/delegation/` owns 16 `Change*` symbols, the
`delegation_change_requests` table, and a live `POST …/delegations/{id}/changes` route, where
it means *a revision of a feature diff under review*. Since a change request owns a task via
`task_id`, the rename produces `delegation_change_requests.sandbox_change_id` — a change row
owning a change row.

The hierarchy it was planned from is also wrong. It draws `WorkItemRun → Task` as one line.
A Task's only foreign key is `tasks.sandbox_id`; three things open one:
`work_item_runs.task_id`, `delegation_change_requests.task_id`, and a standalone
`POST /tasks`.

Measured cost had it run: 1,139 occurrences over 63 identifiers in `backend/app`, 1,266 over
85 in `backend/tests`, 25 in `frontend/src` that are all wire fields and would not change.
Six OpenAPI component names would move.

Done instead: the six missing implementation-chain terms are now defined in `CONTEXT.md`
with a nesting diagram drawn from the foreign keys.

**If the rename is ever wanted**, pick a free word — `SandboxEdit` reads clean at
`delegation_change_requests.sandbox_edit_id` — or free `change` first by renaming the
delegation family, which touches the `/changes` route and so is a wire change.

---

## 6. Working agreements that earned their place

These came out of eleven phases. Each one is here because ignoring it cost something.

- **Verify every figure and every scoping claim before planning from it.** Every phase found
  the plan's module list or counts wrong. Report corrected numbers explicitly.
- **A green suite is not proof a test covers anything.** Eleven instances so far. One phase
  ran a green 834-test suite with the project's real `.env` file silently not loading.
- **Verify a monkeypatch retarget by DELETING the patch**, not by aiming it at a module that
  binds no such name — the latter raises from `monkeypatch.setattr` itself and so fails for
  the wrong reason. If the test still passes without the patch, check whether it is green for
  an environmental reason before concluding the patch is cosmetic.
- **Any move that changes a file's depth must be checked for `__file__`-derived constants.**
  Grep `__file__`, `Path(`, `parents[`, `parent.` before moving. A depth change once
  rewrote `ENV_FILE` silently and 834 tests stayed green.
- **Diff the OpenAPI dump, not just the source.** Pydantic copies a model's docstring into
  the schema as its `description`, so an internal note becomes public API documentation.
  Caught that way on 19 Aug 2026; unreadable from the diff.
- **The frontend gate is `npm run build`, not `tsc --noEmit`.** Only the former type-checks
  the test sources. A break reached `main` because of that difference.
- **Never mix a file move with a behaviour change** in one commit.
- **When removing or moving code, let the tests break loudly.** Repoint the test at the
  module that now binds the name. A shim, alias or re-export is the wrong fix.
- Every change leaves both suites green and the repo shippable.
- ASD-STE100 plain language, see `CLAUDE.md`. Real figures, or write "unmeasured".

### The oracle method, for any large mechanical change

Dump source, constants and the OpenAPI schema over all app modules before and after, then
diff. It catches value-level defects a source diff cannot.

1. **Include a negative control.** A function moved between modules must be *invisible* to
   the oracle. Without that test you cannot tell a move oracle from a change detector, and
   that is the property the whole method depends on.
2. **The constants dump earns its keep.** It caught the one real defect of phase 9. A source
   diff could not have.
3. **Expect `constants_qualified` to fire on legitimate relocation.** Confirm the diff is key
   renames carrying byte-identical values. A same-key *value* change is the real signal.
4. Normalise sets to sorted and regex out ` at 0x[0-9a-f]+`.
5. Type aliases and `logging.Logger` objects embed their module path in their `repr`, so they
   diff on any move. Expected, not a defect.
6. **Re-run a mutation harness before trusting the oracle on new work.** A byte-identical
   oracle proves nothing until you have shown it can fail.
