# Open work

**State:** `chore/open-work-section-3` @ `93add0c`, 20 Aug 2026
**Suites:** backend 835 passed, 43 skipped, ~33s. Frontend 80 passed, `npm run build` clean.
**Lint:** `ruff check app tests` passes. `ruff format --check` reports 219 files formatted.

This replaces `architecture-review-verification-and-plan.md` and
`ai-orchestrator-architecture-review-consolidated.md`. Both are deleted. The plan they
described is complete: phases 0 through 10 are resolved, and phase 3.4 is closed by
ADR 0009. Their per-phase status blocks live in the git history and in the commit
messages that landed each phase.

Section 3 is cleared. Six commits did it, `60d743d` through `93add0c`; the ruff work is
four of them and is much larger than section 3 implied. Section 3 below now records what
landed instead of what is open.

Every figure below was re-measured on `93add0c`. The method for each is given, so the next
reader can re-measure rather than trust. Note that the backend line counts moved: `ruff
format` rewraps, so most files grew.

---

## 1. Never covered by any phase

### The two largest backend modules were never decomposition targets

```text
1,713  app/sandboxes/database.py
1,547  app/sandboxes/service.py
1,347  app/planning/service.py
1,262  app/delegation/execution.py
1,082  app/tasks/service.py
  981  app/previews/service.py
```

Five of the six grew, by 2 to 65 lines. That is `ruff format` rewrapping, not new code.
`previews/service.py` is the exception and it dropped out of the top five: it fell 1,097 to
981 because it carried 57 unused imports, all phase 8 leftovers whose code had moved to
`resources.py`, `sharing.py`, `dependency_cache.py` and `health.py`. Nothing had noticed.

Phase 7 split `ControllerStore`; phase 8 split `previews`. Nothing did this. Phase 5 grew
`sandboxes/service.py` from 695 to 1,487 lines by design and recorded it; it is 1,547 now,
after formatting.
`sandboxes/database.py` also holds one of the two shared-database implementations
that section 4 closes.

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

### Planning reconcile has no Docker-down coverage

Exposed by clearing section 3, not introduced by it.
`tests/planning/test_reconcile.py` carried a test named
`..._when_docker_is_down` whose every assertion ran before any Docker call. It passed with
its Docker patch deleted. The test is now renamed to what it actually asserts, which leaves
the real gap visible: **nothing covers `reconcile_controller_state` when the daemon is
unreachable.** Writing that test needs a decision about whether to fake the daemon or gate
it behind the Docker suite above.

---

## 2. Structural debt, measured

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
app/controller/store/projects.py:23    register_v1_project
app/delegation/execution.py:1254       _provider
app/delegation/service.py:415          view
app/planning/service.py:1208           _generated_at
app/sandboxes/lifecycle.py:178,179,191 _stop_blocking_preview
app/sandboxes/lifecycle.py:251,270     drain_sandbox_writers
app/sandboxes/service.py:1249          resume
app/startup.py:92                      _reject_abandoned_tasks
app/tasks/service.py:208               run_task
app/tasks/service.py:752,753           _stop_task_preview
```

Nine sit inside the 8-node cycle and are plausibly load-bearing. The `startup.py:92` one is
the candidate worth testing first: `startup` is acyclic now, so the cycle it plausibly
dodged is cut. Unproven, and hoisting it is a behaviour change.

### `PreviewStatus` was never added

Carried from phase 8. Preview status is still strings. Nothing has established which states
are genuinely read, and that analysis has to come first.

---

## 3. Cleared, 20 Aug 2026

All five items are done, on `chore/open-work-section-3`. Kept here because two of them
returned findings worth reading, not to claim credit.

| Commit | What landed |
|---|---|
| `60d743d` | The four code items below |
| `fb88125` | ruff added as a gate, no code change |
| `48fff63` | 239 safe autofixes |
| `542f94e` | `ruff format`, 129 of 219 files |
| `6c78308` | Two dead duplicates ruff found |
| `93add0c` | The remaining 115 findings |

- **Three dead imports** in `tests/delegation/test_delivery.py`: gone.
- **The mis-named reconcile test** is now
  `test_reconcile_settles_an_interrupted_turn_and_releases_it`, and its Docker monkeypatch
  is deleted. Confirmed the doc's claim first by deleting the patch and re-running: it
  passes in 0.82s without it. **The Docker-down path it claimed to cover was never covered
  and still is not.** That gap is real and now sits in section 1.
- **`tests/controller/test_lifecycle.py`** moved to `tests/test_startup.py`. It draws its
  helpers from `tests/conftest.py`, so the depth change needed no other edit.
- **The frontend layering oddity** is fixed. `PhaseAgent` moved out of
  `pages/planningSessionModel.ts` into `components/planningAgentInspectorModel.ts`,
  matching the existing `components/delegationWorkspaceModel.ts` convention.
- **`ruff` is installed and green.** See below; it was not a small item.

### ruff was four commits, not a bullet

The entry read "install it or stop asking agents to run it", which implied a one-line fix.
Measured on first run: **353 findings and 132 of 219 files needing reformat.** Pinned to
`ruff>=0.16,<0.17` in a `dev` extra, because ruff widens its default rule selection between
minor releases and would otherwise change the gate without a commit. `line-length = 88`,
ruff's default, chosen because the code already fits it: the 99th percentile line was 89
characters.

The count moves as you work, because fixing one finding exposes another, so read it as a
sequence rather than one total:

| Step | Findings |
|---|---|
| First run, no config | 353 |
| After the config landed | 350 |
| `ruff check --fix` | 356 seen, 239 fixed, 117 left |
| The two real defects removed | 115 left |
| Manual pass | 0 left |

That last 115 went: **67 code changes, 25 inline `noqa` each carrying its reason, and 23
`F811` covered by one per-file-ignore** for `tests/**`, where pytest imports a fixture and
then injects it by parameter name. No `except` clause was narrowed to satisfy a rule; the
broad catches in teardown and best-effort paths took a `noqa` instead.

`ruff format` reformatted 129 files, not the 132 first measured. The autofix pass had
already brought three of them into shape.

### The one real defect the gate found

`app/controller/store/events.py` ended with an orphan copy of the store cache:

```python
_stores: dict[Path, ControllerStore] = {}
```

Nothing read or wrote it. The live cache is `app/controller/store/__init__.py:50`, with its
`RLock` and accessor beside it. The orphan is a phase 7 leftover.

It was not inert. On Python 3.14.4, PEP 649 defers annotation evaluation, so the module
imported fine while reading its annotations raised `NameError`: neither `Path` nor
`ControllerStore` is imported in that file. Any caller of `typing.get_type_hints` on it
would have failed.

**Eleven phases and 835 tests never touched that module's annotations, so it stayed green
over a latent `NameError` the whole time.** `F821` is what surfaced it. This is the
strongest instance yet of the section 5 rule that a green suite proves nothing about code
no test reads.

Also removed: `app/previews/router.py` imported `LOG_READ_TIMEOUT_SECONDS` from
`app.platform.log_stream`, then redefined it locally. Both were `0.5`, so nothing changed
behaviour, but the import read as live and was not.

---

## 4. Decided and closed — do not reopen without reading this

### Phase 3.4, two shared-database implementations — closed separate, 20 Aug 2026

Decided in `docs/adr/0009-two-shared-database-implementations.md`. Keep both
`previews/sharing.py:133 _shared_database_server` and
`sandboxes/database.py:1393 _ensure_shared_server`. Change no code.

They are not duplication. `sandbox_database_runtime` (`sandboxes/database.py:1053`)
routes a preview to the previews path whenever the sandbox has
`lifecycle_version != "v1"` or `db_engine == "none"`, and to the sandboxes path
otherwise. Previews is MySQL-only by construction, so it cannot address a PostgreSQL
shared server at all.

Two of the five behavioural differences the old entry listed are not behavioural.
`database=` is unread when `shared=True` (`sandboxes/database.py:257`), and container
start is identical because `create_hardened` returns the container unstarted
(`containers/hardened.py:180`). The `LABEL_DATA_MANAGED` volume difference is inert
too: its only reader also filters on a run id, which shared volumes never carry.

Three differences remain, and only the image source can cause harm. The shared
container name keys on the project, so one project with a `mysql` sandbox and a
`none` sandbox points both implementations at one container; mismatched image tags
then give one of them a permanent 409.

Measured on 20 Aug 2026, the collision is latent. Both the live store and the 18 Aug
pre-reset backup hold one project and one sandbox (`v1`, `db_engine = none`), zero
`sandbox_databases` rows and zero `shared_database_schemas` rows. All four recorded
approvals declare `services: {}`. Neither implementation has ever provisioned a
database here.

**If the feature is ever used**, give the image one authority. Do not merge the two
functions — that leaves the shared container name, which is the part that collides.

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

## 5. Working agreements that earned their place

These came out of eleven phases. Each one is here because ignoring it cost something.

- **Verify every figure and every scoping claim before planning from it.** Every phase found
  the plan's module list or counts wrong. Report corrected numbers explicitly.
- **A green suite is not proof a test covers anything.** Twelve instances so far. One phase
  ran a green 834-test suite with the project's real `.env` file silently not loading. The
  latest is the strongest: `app/controller/store/events.py` held a latent `NameError` in its
  module annotations across eleven phases, because no test ever read them.
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
- **The backend gate is `ruff check app tests` plus `ruff format --check`.** Both are green
  as of `93add0c` and must stay that way. Run them from `backend/.venv/bin/ruff`. Do not
  reach for `--unsafe-fixes`, and never narrow an `except` clause to satisfy a rule: add a
  `noqa` with the reason instead.
- **A lint rule that fires 20-plus times is usually one pattern, not 20 defects.** Read a
  handful before fixing any. Of ruff's 353, the 34 `ISC004` were all deliberate multi-line
  prompt strings and the 23 `F811` were all one pytest fixture idiom. Two of the 353 were
  real defects: one `F821` and one `F811`, both single occurrences, both dead code left by
  an earlier split phase. The value of the gate was in those two, not in the other 351.
- **Verify a mechanical string rewrite with a string-constant oracle.** Count every `str`
  constant per file before and after and diff. The suite cannot tell you that 34 rewrapped
  prompts are byte-identical. Three files differed and each had to be accounted for by
  hand.
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
