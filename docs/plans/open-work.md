# Open work

**State:** `main`, 21 Aug 2026, pushed and level with `origin/main`. **This header carries no
commit hash on purpose.** A tracked file cannot state its own commit accurately, because
recording the hash changes the hash. Read `git log` for the exact HEAD. The figures below are
measured after the preview size-limit sweep recorded in section 2.
**Suites:** backend 854 passed, 43 skipped, ~27s. Gated backend 893 passed, 4 skipped, ~174s.
Frontend 80 passed, `npm run build` clean — **not re-run since `189a840`; no
frontend file has changed since.**
**Lint:** `ruff check app tests` passes. `ruff format --check app tests` reports 252 files
formatted.

This replaces `architecture-review-verification-and-plan.md` and
`ai-orchestrator-architecture-review-consolidated.md`. Both are deleted. The plan they
described is complete: phases 0 through 10 are resolved, and phase 3.4 is closed by
ADR 0009. Their per-phase status blocks live in the git history and in the commit
messages that landed each phase.

Section 3 is cleared. Six commits did it, `60d743d` through `93add0c`; the ruff work is
four of them and is much larger than section 3 implied. Section 3 below now records what
landed instead of what is open.

Every figure below was re-measured on `93add0c`, and section 1 again on `97f98f0`. The method for each is given, so the next
reader can re-measure rather than trust. Note that the backend line counts moved: `ruff
format` rewraps, so most files grew.

---

## 1. Never covered by any phase

### The two largest backend modules were never decomposition targets

```text
1,347  app/planning/service.py
1,262  app/delegation/execution.py
1,082  app/tasks/service.py
  981  app/previews/service.py
  912  app/controller/store/sandboxes.py
```

`database.py` was 1,713 lines and is now a package. Its decomposition is **complete**:
three steps, `27447e2`, `d1216a3` and `0366c17`, all on 20 Aug 2026. Its largest module is
`provisioning.py` at 650 lines and its `__init__.py` is a 201-line facade, so it has left
this table. See "Decomposing `sandboxes/database.py`" below for the measured seam, the
method, and the four findings the three steps produced.

`sandboxes/service.py` was 1,547 lines and is now a package too, after three steps on 20 Aug 2026, so it has also left this table. Its largest module is
`transitions.py` at 485 lines and its `__init__.py` is a 70-line, 19-name facade. **No
backend module is over 1,400 lines any more.**

Measured on `93add0c`, five of the six had grown by 2 to 65 lines against the previous
reading. That was `ruff format` rewrapping, not new code. `previews/service.py` was the
exception and it dropped out of the top five: it fell 1,097 to 981 because it carried 57
unused imports, all phase 8 leftovers whose code had moved to
`resources.py`, `sharing.py`, `dependency_cache.py` and `health.py`. Nothing had noticed.

Phase 7 split `ControllerStore`; phase 8 split `previews`. No phase touched these two.
`database.py` is now being split outside the phase plan, one step at a time. Phase 5 grew
`sandboxes/service.py` from 695 to 1,487 lines by design and recorded it; it reached 1,547
after formatting, and is now a package whose largest module is 485 lines.
The `sandboxes/database/` package also holds one of the two shared-database implementations
that section 4 closes.

For contrast, the frontend hotspots the review named were fixed: `DelegationWorkspace.tsx`
went 1,780 → 525 lines, `PlanningSessionPage.tsx` 1,200+ → 635.

### Decomposing `sandboxes/service.py` — done, 20 Aug 2026

Scoped at `eb35b56`. All three steps landed on 20 Aug 2026. The decomposition is
**complete** and needs no follow-up. Every figure below was measured from the tree by an AST
pass, not carried over from a handoff.

**The module.** 1,547 lines: 75 lines of imports, 34 top-level definitions totalling 1,406
lines, and **no module-level state**. The split has no shared mutable globals to preserve.

**External surface.** Six files import it. `app/sandboxes/router.py` takes 12 names by direct
import and reaches 7 entrypoints by `service.X` attribute. Four test files import it as
`sandbox_service` and monkeypatch it: `tests/sandboxes/test_router.py`, `test_sync.py`,
`test_staleness.py` and `tests/delegation/test_delivery.py`. The module exposes 91
attributes; about 21 are reached from outside.

**The seam.** The internal call graph is a layered DAG with no cycles. Ten cohesive groups:

```text
   48  errors        6 exception classes, zero dependencies
   46  outcomes      6 result dataclasses
   51  coercion      require_v1 + 6 private helpers
  189  provisioning  complete_database_provision, reset_database
   95  engine        confirm_engine, _confirm_engine_snapshot
  429  lifecycle     create_or_resolve, resume, destroy, _sweep_manifest_resources
   58  resources     _docker_collection, _remove_manifest_resource, remove_orphan_resource
  213  sync          sync, sync_engine_report
  201  publish       publish
   76  staleness     staleness
```

Each group pulls a distinct set of external packages, so the grouping is cohesion, not
alphabetical tidiness. Two helper sets are shared and must sit in a lower layer:
`complete_database_provision` has five callers across four groups, and `_docker_collection`
with `_remove_manifest_resource` are shared by `destroy` and `remove_orphan_resource`.

**Shape.** `service.py` becomes the package `app/sandboxes/service/` with a facade
`__init__.py`. It cannot be flat modules — `lifecycle.py`, `publish.py` and `git.py` already
exist in `app/sandboxes/`. This is the `database/` shape.

**Where it will bite.** **15** names are monkeypatched on the service module across **50**
sites in 4 test files. Every one needs a retarget. A grep-based count during scoping said 10
names and 16 sites; it was wrong, because `monkeypatch.setattr(` often puts the module on one
line and the name on the next. **Enumerate patch sites with an AST pass, never with grep.**

```text
name                             used by                                    destination
require_clean_workspace          sync                                       sync
create_workspace_safety_ref      sync                                       sync
restore_workspace_safety_ref     sync                                       sync
mirror_base_commit               sync                                       sync
sync_workspace_from_mirror       sync                                       sync
fetch_canonical_mirror           sync, staleness                            two modules
count_mirror_staleness           staleness                                  staleness
ensure_project_mirror            create_or_resolve                          lifecycle
ensure_workspace_import          create_or_resolve, resume                  lifecycle
verify_workspace_identity        resume                                     lifecycle
reviewed_target                  publish                                    publish
publish_reviewed_feature         publish                                    publish
discover_or_create_pull_request  publish                                    publish
discover_engine                  sync_engine_report, create_or_resolve,
                                 resume                                     three modules
complete_database_provision      sync, create_or_resolve, confirm_engine,
                                 reset_database, resume                     five modules
_remove_manifest_resource        remove_orphan_resource,
                                 _sweep_manifest_resources                  two modules
```

Each site resolves to whichever module holds the entrypoint that test drives. That is a
property of today's tests, not of the structure. `complete_database_provision` reaching five
modules and `discover_engine` reaching three are the "one patch site becomes two" shape that
bit the database refactor three times.

**Step 1 is not retarget-free.** `_remove_manifest_resource` is patched at
`tests/sandboxes/test_router.py:1035`. Step 1 moves it and `remove_orphan_resource` into
`resources.py` while its other caller, `_sweep_manifest_resources`, stays in `__init__.py`.
The patched test drives the orphan-removal route, so that one site retargets to
`sandbox_service.resources`. One retarget, and it must be proved by `__globals__`.

`KNOWN_CYCLE` stays where it is — 5 since `ba65624`. This is an intra-package split.

**Decisions taken, 20 Aug 2026.** The user chose all three:

1. **Prune the facade** to the names router and tests actually reach. A wide facade would let
   a stale monkeypatch silently patch a dead binding while the real code runs. A pruned
   facade turns that into a loud `AttributeError`. Same policy as the database facade.
2. **`lifecycle` stays one module** at 429 lines. Splitting it would add two more retarget
   destinations for `discover_engine` and `complete_database_provision`.
3. **Three steps**, the database cadence:
   - Step 1: the leaves — errors, outcomes, coercion, resources. One retarget.
   - Step 2: provisioning, engine, and the entrypoint modules. Carries the other 49
     retargets.
   - Step 3: reduce `__init__.py` to the pruned facade.

**Steps 1 and 2, as landed.** The package is now:

```text
  70  __init__.py           facade only, 19 names, zero definitions
 485  transitions.py        create_or_resolve, resume, destroy, _sweep_manifest_resources
 247  syncing.py            sync, sync_engine_report
 234  publishing.py         publish
 216  provisioning.py       complete_database_provision, reset_database
 113  engine.py             confirm_engine, _confirm_engine_snapshot
  88  mirror_staleness.py   staleness
  77  resources.py          remove_orphan_resource + 2 private helpers
  71  coercion.py           require_v1 + 6 private helpers
  59  outcomes.py           6 result dataclasses
  58  errors.py             6 exception classes
1718  total
```

`sandboxes/service.py` has left the over-1,400-line table. The largest module is
`transitions.py` at 485 lines. The 1,935 against 1,547 is duplicated import headers, the
same tax the `database/` split paid.

Names avoid the three collisions with modules the facade already imports from:
`transitions.py` not `lifecycle.py`, which holds locks and leases; `publishing.py` not
`publish.py`; `engine.py` sits beside `engine_detection.py`.

**Finding: a submodule must never share a name with a function it exports.** Step 2 first
named the module `staleness.py` while the function it holds is also `staleness`. The facade
ends with a `globals().pop(...)` block that drops submodule names from the package namespace,
copied from `database/__init__.py`. `globals().pop("staleness")` therefore deleted the
**function**, and `service.staleness(...)` in the router broke. Renamed to
`mirror_staleness.py`, which is the more honest name anyway. **The surface probe caught this,
the test suite did not catch it first**, because the probe compares against a captured
baseline instead of asking whether anything crashes. Check every new submodule name against
the names it exports before writing the pop block.

**Finding, since corrected: the "ten inert patch sites" figure was wrong.** A delete-test
over all 50 retargets — flip one (file, module, name) group back to the facade, run that
file, require a failure — reported 6 groups as inert: all five sites in
`tests/delegation/test_delivery.py`, and the five `complete_database_provision` sites in
`tests/sandboxes/test_sync.py`. **That run was ungated, and `test_delivery.py` is behind
`RUN_DOCKER_PREVIEW_TESTS=1`, so its five sites scored inert only because the test was
skipped.** The trap was written down in the same breath as the figure, and the figure was
recorded anyway. Re-measured gated on 20 Aug 2026, three of those five bite.

The retargets themselves were correct all along, proved twice: `__globals__` identity for
every entrypoint, and a counting wrapper on the real functions that recorded **zero** real
calls with the retargets in place.

**Closed by `ac144d6`, 20 Aug 2026.** See "The inert patch sites, measured and closed" in
section 3.

**Method note: "the test still passes without the patch" is not one question but two.**
Deleting a patch and finding the test still green only says the real function is harmless
in that fixture — here it succeeds against the fake Docker client. The question that finds
a weak test is different: neuter the *replacement value* and see whether anything notices.
Of the five `test_sync.py` sites, all five reached the patched function, and deleting the
patch passed every time, but neutering the substitute failed only three. Those two numbers
disagreeing is the gap. Run both probes, and read them separately.

**Two traps in the delete-test harness itself.** Run it ungated and every gated test is
skipped, which scores as inert — that is the error above, and it is worth more than one
reading. And once step 3 pruned the facade, flipping a patch back to `sandbox_service`
raises `NameError` because the file no longer imports it, so every group scores as biting
for a trivial reason. The delete-test was only meaningful in the window between step 2 and
step 3. **After a prune, delete the patch instead of flipping it.**

**Step 3, the facade prune — done.** `__init__.py` went from 287 lines and 91 re-exported
names to **70 lines and 19 names**: exactly the set `app/sandboxes/router.py` reaches, by
direct import or as `service.<name>`.

```text
EngineConfirmation          SandboxUnavailable          destroy
SandboxConflict             SandboxValidationError      publish
SandboxDependencyFailure    _json_value                 remove_orphan_resource
SandboxInternalFailure      _optional_string            require_v1
SandboxNotFound             confirm_engine              reset_database
                            create_or_resolve           resume
                                                        staleness
                                                        sync
```

The keep-list is 19, not the 21 that scoping predicted. Three names were reached only by
tests, and the user chose to move those four call sites onto the owning module instead of
holding them on the facade: `complete_database_provision` to `provisioning`,
`sync_engine_report` to `syncing`, and two `ensure_workspace_import` reads to `transitions`.
`tests/sandboxes/test_router.py` and `test_sync.py` no longer import the facade at all.

That choice is what finishes the job. **All 15 monkeypatched names are now absent from the
facade**, so a patch aimed at the old target raises `AttributeError` instead of silently
patching a binding nothing resolves through. Had the facade been pruned first, both step-2
findings above would have surfaced as loud failures rather than through a probe. Keeping
`complete_database_provision` would have left that hazard half-open for the one name whose
patches were already proved inert.

The prune moved no definitions. The AST dump is byte-identical across the step.

### Decomposing `sandboxes/database.py` — done, 20 Aug 2026

All three steps landed on 20 Aug 2026: `27447e2`, `d1216a3`, `0366c17`. The 1,713-line
`app/sandboxes/database.py` is now the package `app/sandboxes/database/`, and its
`__init__.py` is a pure facade holding zero `def` and zero `class` statements.

```text
  650  provisioning.py  the orchestration layer, 15 definitions
  349  mysql.py         MySQLDatabaseEngine
  201  __init__.py      63 re-exports, nothing else
  195  postgres.py      PostgreSQLDatabaseEngine
  164  shared.py        shared-server lock, naming, identifier, statement, health, hash
  147  _engine_ops.py   sqlite volume, server credentials, database command runner
  139  contracts.py     ErrorFactory, runtime, six request dataclasses, DatabaseEngine
  128  sqlite.py        SQLiteDatabaseEngine
   25  registry.py      four engine singletons, DATABASE_ENGINES, database_engine
   15  constants.py     the eleven module-level literals
    9  errors.py        SandboxDatabaseError, SandboxMigrationError
```

**The seam was measured before anything moved,** with an AST pass that built the internal
reference graph over 14 regions. The result is why this was safe to do incrementally: the
graph is a **DAG**, and the engine layer never calls the orchestration layer. The file was
not tangled. It was two stacked layers sharing one file, and that held for all three steps.

Final layering, verified acyclic with no module importing its own package:

```text
constants <- contracts <- shared
                   \
                    _engine_ops <- mysql <- postgres
                                       \ <- sqlite
                                            registry <- provisioning <- __init__
```

`postgres.py` and `sqlite.py` import `mysql.py` because both engines subclass
`MySQLDatabaseEngine`. That is expected, not a smell.

Findings worth keeping:

- **The import ratchet never moved.** `KNOWN_CYCLE` names top-level packages, not modules,
  so no intra-package split touches it. It stayed at 8 through all three steps. If a split
  like this ever appears to move it, something other than a move happened.
- **The `__init__.py` re-export is deliberate,** and is the one documented exception to
  "no shim, alias or re-export". It follows the `app/controller/store/` precedent and keeps
  all eight consumer modules untouched. The rule still holds for everything else.
- **The facade preserves consumers, not the whole namespace.** Steps 2 and 3 dropped 55
  names that only the moved code had imported: 17 in `d1216a3` (`Mount`, `run_hardened`,
  the `Hardened*` specs, `Capabilities`, `Capture`, `Egress`, `create_hardened`,
  `ContainerError`, `ReadTimeout`, `MYSQL_PORT`, `POSTGRES_PORT`, the two
  `DATABASE_COMMAND_*` literals, `base64`, `json`, `re`) and 38 in `0366c17` (thirteen
  stdlib names, five docker names, the seven `LABEL_*` literals, `ControllerStore`,
  `PreviewSettings`, `PreviewConfiguration`, `PreviewDependencyService`, `NO_DATABASE`,
  `ensure_image`, `database_name`, `db_data_volume`, `workspace_volume`, `ownership_labels`,
  `validate_ownership`, `sandbox_network_name`). An AST scan over `app/` and `tests/` found
  no importer of any of them from this package. **Decided on 20 Aug 2026:** incidental
  imports may leave the surface once proved unused; deliberate re-exports may not. The
  facade now carries exactly the 63 names the package itself defines.

- **The test trap fired in step 2, wider than predicted, and again in step 3.** The original
  note said only step 3 was at risk, and that the `_run_database_command` patch in the
  `_ensure_shared_server` test was already dead because that function has no direct call to
  the helper. **Both halves were wrong.** `_ensure_shared_server` reaches the helper
  indirectly, through `database_engine(...).provision(...)`. Reading a function's own body
  is not enough; follow the call graph.

  One patch site also became two. `MySQLDatabaseEngine.provision` calls
  `_run_database_command` through the binding in `mysql.py`, and *also* calls
  `_read_or_create_server_credentials`, which resolves the same helper inside
  `_engine_ops.py`. Before the split both were one module and one patch covered both. A
  third site, in the `test_router.py` fixture, was listed in no earlier note and needed
  splitting across `mysql` and `postgres`.

  **The rule:** patch every module that binds the name *and* sits on the call path, never
  the package facade. Enumerate those modules from the call graph, not the caller's body.
  The cheapest reliable check is `func.__globals__` — a function's globals dict *is* the
  namespace its names resolve in, so `f.__globals__ is target.__dict__` settles the
  question without running anything.

- **A passing revert does not clear a retarget.** Step 3 retargeted five patches. Reverting
  `_wait_for_server_health` to the facade left the test green, which looks like proof the
  patch is cosmetic. It is not: the real health wait simply succeeds harmlessly against the
  fake Docker client. A tripwire on the real binding showed it reached once before the
  retarget and zero times after. That tripwire also caught a retarget the delegate had
  silently skipped — four of the five were done, and the suite was green either way. When a
  revert passes, install a tripwire before concluding anything.

- **A guard patch cannot be proved by reverting it.** The `database_engine` patch in
  `test_sandbox_shared_server_refuses_an_existing_container_with_another_image` raises on
  call, asserting provisioning never starts. The test passes with the patch deleted, because
  it is never invoked. Prove that kind of patch by `__globals__` identity instead.

Verification method, used on all three steps and worth repeating for any package split:
compare every top-level definition by `ast.dump` between the old tree and the new tree, then
diff a normalised public-surface probe of the package. The AST comparison is the check that
cannot be gamed and should be the primary one; it was clean on all three steps. The surface
probe is what actually caught things — the 17 pruned names in step 2 and the exact 38 in
step 3. Run both. See section 5.

---

### Runtime behaviour: the gated suite is green again

Preview execution modes, Docker reconciliation, and publish are now exercised for real.
The gated suite ran on 20 Aug 2026 for the first time since `ce205cd`, four handoffs back.

Baseline on `9b729e2` was 869 passed, **5 failed**, 4 skipped in 147s — exactly the five
failures recorded at `ce205cd`, so they were rot and nothing had regressed since. After
`97f98f0`: **874 passed, 4 skipped, ~139s.**

The rot was **six tests, not five.** The sixth is the one worth reading:

`test_native_preview_reports_real_container_and_dependency_durations` passes cold and fails
warm. It hardcodes the sandbox id `"timing-sandbox"`, and `_data_volume`
(`previews/dependency_cache.py:292`) keys the *persistent* npm cache on sandbox id, not run
id. Run 1 creates the cache. Run 2 finds it populated, short-circuits with "already
installed ... skipping install", and emits 1 dependency event where the test asserts 2.
**Eleven phases never saw it because nobody ran the suite twice in a row.** Proven by
deleting that one volume and re-running: green.

So the standing instruction is now: **run the gated suite twice, not once.** A single green
run cannot distinguish a repeatable suite from one that poisons its own next run.

The five documented failures were two causes, not five. Four were the same missing project
registration; one was a stale assertion on a refusal string. Both are described in `97f98f0`.

### The leak is much smaller than recorded

Measured over three gated runs on 20 Aug 2026: **0 containers, 0 networks, 6 to 8 volumes
per run.** Every leaked volume is anonymous and hash-named; after a run no named
`orchestrator-*` volume survives. The old entry claimed "roughly 2 containers, 5 networks
and 30 volumes each", and warned that 28 leaked `orchestrator-preview-*` networks had
exhausted Colima's address pool. **Neither reproduced.** Network count held at 4 across all
three runs.

Prune by the exact prefix `orchestrator-preview-` or `orchestrator-deps-`, never by
`name=orchestrator`. The broader filter also matches
`orchestrator-agent-auth-<provider>-<profile>-<digest>`
(`agents/service.py:521`), which persists agent CLI logins. Deleting those costs a
re-authentication per profile. That mistake was made on 20 Aug 2026 and cost the `claude`
and `codex` default profiles.

### Disproved: planning reconcile Docker-down coverage

An earlier entry here claimed **nothing covers `reconcile_controller_state` when the daemon
is unreachable.** That is wrong. Four tests in `tests/test_startup.py` cover it:
`test_startup_closes_every_open_agent_writer_session`,
`..._reclaims_a_lease_for_a_settled_operation`, `..._reclaims_a_stale_unsettled_lease`, and
`..._orphan_reporting_degrades_when_docker_is_unavailable`. Each patches `app.startup.docker`
so `from_env()` raises `DockerException`, then asserts the degraded counts.

Proven by deletion on 20 Aug 2026, not by reading: changing `app/startup.py:227` from
`return counts` to `raise` fails exactly those four and no others. `app/startup.py` was
restored with zero diff.

The claim came from reading `tests/planning/test_reconcile.py` alone. The function lives in
`app/startup.py`, and its tests moved to `tests/test_startup.py` in `60d743d` — the same
commit that wrote the claim. **A gap found in one file is not a gap until you have looked
where the code actually lives.**

---

## 2. Structural debt, measured

Three of the four items here are done, 20 Aug 2026, and keep their entries because each
returned a finding. **What is still open is the 2-node import cycle, one cut from gone.** The function-local
imports are no longer part of it: 1 of the original 14 remains and it is intra-package. The three undefended
vocabularies — Docker labels, preview status, and the `active_preview` argument — were
**decided on 20 Aug 2026** and are now partly closed; see their entry below. What remains
of them is behavioural path coverage for four statuses, deferred on purpose.

Done 20 Aug 2026: the preview-status write sites (`4fe0e37`), the dead `rebuilding` status
and migration 32 (`d0f0d07`), the `startup.py` import hoist (`a787fdc`), the remaining
11 hoistable function-local imports, stage 1 of the cycle (`6e5a30b`) and stage 2 (`ba65624`).

### The domain import cycle — 2 nodes, was 8

`previews, sandboxes`. **2 intra-cycle edges**, was 29.

**The "15 intra-cycle edges" recorded here for the 5-node graph was wrong; it was 14.**
Re-measured with `/tmp/cycle_edges.py`. Assume the next figure on this page is wrong too.

Phase 9 cut it from 10 nodes to 8. Stage 1 (`6e5a30b`, 20 Aug 2026) cut it to 6 by removing
`delegation` and `implementation_context`. Stage 2 (`ba65624`) cut it to 5 by removing
`planning`. Stage 3 (`be81e44` and `148d907`) cut it to 4 by removing `tasks`. Stage 4 (`7fe6ee8` and `0b05b89`)
cut it to 3 by removing `agents`. Stage 5 (`aba2eeb` and `5a5a25b`) cut it to 2 by removing `projects`. See below
for what each took.

**The old entry claimed "no further file move can shrink it". That was wrong**, and it was
wrong in the direction that stops work: it argued the whole cycle needed signature changes.
Two of the three stage-1 edges were cut by moving a symbol to the package that owns it. Only
the third needed a new seam. Re-measure before believing any claim about what is left.

**A wrong count costs a re-plan. A wrong blocking claim costs the work entirely**, because
nobody scopes a task the page says is impossible, so the claim is never tested. Of the seven
edges cut across stages 1 and 2 and `be81e44`, six were cut by moving a symbol to the package
that owns it; stage 3's remaining edge was three more such moves. Treat "this cannot be done
cheaply" as the least-tested sentence on any page.

**What is left is measured, not asserted.** The exact minimum feedback arc set for the
original 8-node graph was **8 edges, 25 symbols, 19 files**. Stages 1 to 5 removed 27 of the
29 intra-cycle edges. Which of those were members of that original minimum set has not been
re-derived, so do not quote a "spent" count — re-measure the current graph instead.

**This page has predicted the remaining work wrongly three times now.** It said the cycle
could not shrink without signature changes; it did, nine times over. It said `previews <->
projects` was all that was left; `sandboxes -> agents` was sitting there at 2 symbols. It
then said `previews -> projects` was the cheapest next cut; the actual cut was
`projects -> sandboxes`, one symbol, which only became visible after an unrelated file
moved. **Re-measure with `/tmp/cycle_cuts.py` before believing the paragraph below.**

**One edge is left, not one cut.** It is a 2-cycle, so severing either direction ends it —
but neither direction falls to a single move. Re-measured 20 Aug 2026; the per-edge figures
below held, the plan built on them did not. Measured:

| Edge | Cost | Shape |
|---|---|---|
| `sandboxes -> previews` | 2 symbols, **1 file** | `lifecycle.py:21` |
| `previews -> sandboxes` | 17 symbols, **4 files** | 3 sites in `app/sandboxes/database`, 1 in `engine_detection` |

**The "shared primitive filed inside a feature package" reading of this edge was wrong, and
it is corrected here.** Scoped 20 Aug 2026. `app/previews/config.py` is **not** a misplaced
primitive. **All seven existing settings modules live inside the package whose settings they
define** — `agents`, `controller`, `delegation`, `implementation_context`, `planning`,
`previews`, `tasks`. Not every package has one: there are 14 packages under `app/` and 7
`config.py` modules. The claim is about where a settings module goes, not that every package
needs one. The module is exactly where the convention puts it, and most of its contents are preview-owned
policy: `inspection_image`, `default_expiry_minutes`, `proposal_lifetime_seconds`,
`maximum_snapshot_bytes`. **Moving the module is rejected**: it would make `previews` the only
domain whose settings live outside it, and it rewrites 31 importing files (24 under `app/`,
7 under `tests/`) to cut 9 sites. `PreviewSettings` stays in `app.previews`.

**The misplaced symbol is `git_image`, not the module.** It is `alpine/git:latest` under
`PREVIEW_GIT_IMAGE`, and it is read by six packages — `agents`, `delegation`, `previews`,
`sandboxes`, `tasks`, and by parameter in `projects`. A Git container image is not a preview
concept. `app/containers/` already owns Git container execution after stage 5, so it is the
right home. **`app/containers` is not a leaf package** — `containers/git.py:18` imports
`app.controller.config`, and `service.py`, `actions.py` and `router.py` import `app.platform`.
What matters is narrower: **the new `containers/config.py` can itself remain a leaf, and it
imports neither `previews` nor `sandboxes`.**

**Extracting `git_image` cuts 5 of the 12 `sandboxes -> previews` sites and leaves 7. It is
preparation, not the cycle cut.** Do not describe it as the cut. The five fall into two
groups, not five independent edits:

- **Direct callers, drop cleanly:** `service/mirror_staleness.py:37` and
  `service/syncing.py:68` both call `get_preview_settings().git_image` inline.
- **The publish chain, one signature change threaded through three functions:**
  `service/publishing.py:77` -> `publish.py:314` -> `feature_target.py:49`. Each carries a
  whole `PreviewSettings` and reads **only** `git_image`; `publish.py:334` hands it to
  `ensure_target_unchanged`.

**Constraints on the extraction.** Preserve the `PREVIEW_GIT_IMAGE` environment variable
initially, for compatibility. **Do not merge it with `app/implementation_context/config.py:9`**
— that setting is a different image under a different variable (`TASK_GIT_IMAGE`).

**The rewrite count for the extraction is unmeasured.** It depends on the new function
signatures. Do not carry a figure into the plan until it is measured.

**What remains is 1 site**, after `prisma_schema_providers` moved to `app.sandboxes`, the four
settings sites were extracted as `PreviewRuntimeLimits`, and `DatabaseConnectionRequest` was
narrowed. All three are recorded below. The survivor is `stop_preview` plus `stop_task_preview`
at `lifecycle.py:21`.

`lifecycle.py:21` calls into previews at `:180` and `:191` during sandbox teardown. **A callback
inversion does not cut it, and this was traced rather than assumed.** `stop_blocking_previews` is
born at `app/sandboxes/router.py:77` as an API request field and threads through
`publishing.py:39`, `transitions.py:60`, `provisioning.py:176` and `syncing.py:38` into
`lifecycle_lease` at `lifecycle.py:66`. Handing `lifecycle_lease` a teardown callable instead of
a bool **relocates the import from `lifecycle.py` to `router.py`, both inside `sandboxes`.**
Cutting it needs the injection point outside `sandboxes`: either DI wiring in a neutral module,
or moving the stop-then-retry-admission orchestration into `previews` so the router calls
previews before sandboxes. **Both are architectural changes, not refactors.** Grill the options
before touching it.

**Two rejection arguments were tested and failed. Do not reuse either.**

- **"Guard the annotation-only sites behind `TYPE_CHECKING`" does not work, twice over.**
  Neither `database/contracts.py` nor `database/provisioning.py` uses postponed annotations,
  and **0 of 158 files under `app/` use `from __future__ import annotations`** while only 2 use
  `TYPE_CHECKING` at all. Without postponed annotations the class body and the `def` statements
  evaluate those names during import, so a guarded import raises `NameError`. Adding the future
  import to reach the guard breaks a convention held by every other file. And it would not move
  the metric anyway: `tests/test_import_direction.py:101` uses `ast.walk`, which visits imports
  inside `TYPE_CHECKING` and inside function bodies. **Guarded imports still count as edges.**
- **"Extracting settings would rename `PREVIEW_*` environment variables" is false.**
  `app/containers/config.py:19` reads `PREVIEW_GIT_IMAGE` from the `containers` package: the
  variable name survived the package move. The extraction below preserved all four remaining
  `PREVIEW_*` names the same way.

**Preserve `previews -> sandboxes` as the surviving direction.** It matches the domain flow: a
preview operates on a sandbox, so previews depending on sandboxes is the natural dependency.

#### Preview size-limit defaults swept to single constants, 21 Aug 2026 — done

`maximum_dependency_bytes` and `maximum_built_image_bytes` were the last two defaults written
twice, at the dataclass field and again at the `integer_setting` fallback. Each now has one
constant serving both, matching `app/containers/config.py`. **No default is restated anywhere
under `app/previews/` or `app/containers/` now.**

**The drift was the smaller problem. Neither setting was tested at all.** No test in the
repository referenced either field name. Three corruptions, each applied alone, were invisible
to the full 851-test suite:

| Corruption | Effect in production | Caught before | Caught now |
|---|---|---|---|
| misspell `PREVIEW_MAXIMUM_DEPENDENCY_BYTES` | the operator's dependency cap is ignored | no | 1 test |
| misspell `PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES` | the operator's image cap is ignored | no | 1 test |
| drift a field default from its constant | every direct construction gets the wrong cap | no | 1 test |

**The field default and the factory fallback are different seams, and only one sweep test
covers each.** `get_preview_settings` always passes both fields explicitly, so it never
exercises the dataclass field defaults. Those defaults are still load-bearing: **all ten direct
`PreviewSettings` constructions in the test suite omit both fields**, and `resources.py:85`
enforces the built-image cap from one of them. A test that only drives the factory would have
left the third corruption silent — it did, until a separate direct-construction test was added.

**Cost: 3 files** — 1 source, 1 test modified, 3 tests added.

**Verified:** ungated 854 passed, 43 skipped; gated 893 passed, 4 skipped; `probe_all.py`
157 modules, 0 failed; ruff clean over 252 files; OpenAPI byte-identical.

#### `DatabaseConnectionRequest` narrowed to sandbox vocabulary, 21 Aug 2026 — done

`app/sandboxes/database/contracts.py:11` imported `PreviewConfiguration` and
`PreviewDependencyService` to type two fields on one frozen request dataclass. **The scoping
pass is what made this cheap.** Sandboxes read exactly two things off those two Pydantic
models and nothing else: `config.environment[...].from_service`, at `sqlite.py:42`,
`postgres.py:119` and `mysql.py:120`; and `database.database`, a name string used only as the
fallback in `request.database_name or request.database.database`. Two Pydantic models were
being carried across a package boundary to deliver a `str` and a mapping of `str` to `str`.

The contract now names what it uses:

```python
@dataclass(frozen=True)
class DatabaseConnectionRequest:
    service_environment: Mapping[str, str]
    database_name: str
    credentials: dict[str, str]
    error: ErrorFactory
```

**`database_name` is required, not defaulted to `""`.** The old empty default was safe only
because of the `or` fallback behind it. With the fallback moved to the caller, a default would
let an empty database name reach a connection URL silently. A required field is the guard.

**Previews owns the translation, at one site.** `_native_service_environment`
(`previews/runtimes/native.py:516`) keeps its signature and its two callers, and builds the
narrowed request from preview models inside its body. The `database_name or database.database`
precedence is preserved there exactly.

**Cost: 7 files** — 4 source under `app/sandboxes/database`, 1 source under `app/previews`,
1 test modified, 1 test added. No error code, message string or connection-URL format changed.

**Effect: `sandboxes -> previews` falls from 4 symbols in 2 sites and 2 files to 2 symbols in
1 site and 1 file.** `KNOWN_CYCLE` is unchanged: the component still holds both packages, and
it will until `lifecycle.py:21` goes.

**Verified:** ungated 851 passed, 43 skipped; gated 890 passed, 4 skipped; `probe_all.py`
157 modules, 0 failed; ruff clean over 252 files; OpenAPI byte-identical.

**The tripwire found a real gap, and it generalises past this change.** Narrowing a contract
does not delete the mapping work — it moves it into a translator, and the translator sits in
the blind spot between the two test suites. `_native_service_environment` had no test anywhere:
the sandbox engine tests in `tests/sandboxes/test_database.py` construct
`DatabaseConnectionRequest` directly and bypass it, and no preview test called it. Two
corruptions, each applied alone:

| Corruption | Before | After |
|---|---|---|
| drop the `database_name` override | silent across 848 ungated and 886 gated tests | fails exactly 1 test |
| map every variable to `""` instead of `from_service` | silent across 848 ungated tests | fails 3 tests |

`tests/previews/test_native_environment.py` closes it, and no test outside that file fires for
either corruption. **Before the change there was nothing to get wrong at that seam; the
refactor created it.** Note the shape is the same as the `PreviewRuntimeLimits` finding
recorded below, one layer down: there an extraction left the composition unasserted, here a
narrowing left the translation unasserted. Both were invisible to a full green suite.

#### `PreviewRuntimeLimits` extracted into `app.containers`, 21 Aug 2026 — done

**The seam was exact, and that is what justified the extraction.** Every one of the twelve
`PreviewSettings` fields was mapped to its reading packages. Eight are read only by `previews`.
**Exactly four are shared with `sandboxes`, and `sandboxes` reads nothing else** —
`preview_memory`, `prepare_timeout_seconds`, `shared_database_memory`,
`shared_database_max_connections`. Extracting them left nothing behind.

**Shape: composition, not a parallel parameter and not a shim.** `PreviewSettings` gained
`limits: PreviewRuntimeLimits = field(default_factory=PreviewRuntimeLimits)`. Previews reads
`settings.limits.<name>`; sandboxes parameters are retyped to `PreviewRuntimeLimits` and read
`settings.<name>` with no prefix. There are no delegating properties. **The router at
`previews/router.py:86` and the 15 previews signatures carrying `settings: PreviewSettings` did
not change**, which is why this cost 20 files instead of threading a second parameter through
all of them.

**Why a direct factory call was rejected.** `previews/router.py` injects `PreviewSettings` via
`Depends(get_preview_settings)`, and tests override behaviour by constructing it. Calling
`get_preview_runtime_limits()` deep in previews would have returned the cached default and
**silently discarded the 120-second timeout at `tests/previews/test_docker_integration.py:241`**
— the one site of ten that sets anything other than the default. It would also have weakened
FastAPI dependency overrides. Configuration resolves at an owning boundary and is passed on.

**`default_factory=PreviewRuntimeLimits`, never `default_factory=get_preview_runtime_limits`.**
Direct constructors must receive deterministic code defaults, not cached environment state. The
environment-backed object is composed only inside `get_preview_settings`.

**Only `preview_memory` was renamed**, to `memory`; `PreviewRuntimeLimits.memory` is clear from
its type, and keeping the old name would have preserved redundant vocabulary. The other three
names are unchanged. **All four `PREVIEW_*` variables keep their names, and
`app/containers/config.py` is now their single Python authority.**

**Four `DEFAULT_PREVIEW_*` constants serve both the dataclass default and the `os.getenv`
fallback**, so no default is restated. **`app/previews/config.py` restated its remaining
defaults** — `maximum_dependency_bytes` and `maximum_built_image_bytes` each wrote the literal
twice, at the field and at the environment fallback. **Swept 21 Aug 2026**; see below.

**Cost: 20 files** — 13 source, 6 tests modified, 1 test added. Nine of the ten test
constructors simply dropped `prepare_timeout_seconds=600`, which is now the default.

**Effect: `sandboxes -> previews` falls from 6 symbols in 6 sites and 5 files to 4 symbols in
2 sites and 2 files.** `KNOWN_CYCLE` is unchanged: the component still holds both packages.

**Verified:** gated 887 passed, 4 skipped, run twice; ungated 848 passed, 43 skipped;
`probe_all.py` 157 modules, 0 failed; ruff clean; OpenAPI byte-identical.

**The tripwire found a real gap, and it is the lesson worth keeping.** Replacing
`limits=get_preview_runtime_limits()` with a bare `PreviewRuntimeLimits()` in the settings
factory makes production ignore all four environment variables. **That corruption passed 847
tests.** The new isolated limits tests in `tests/containers/test_config.py` did not catch it,
because they exercise the factory directly and never assert that `get_preview_settings`
composes it. `tests/previews/test_config.py` was added to close that, and the same corruption
now fails exactly one test. **A settings extraction needs a composition test, not only a
factory test** — otherwise the seam between the two objects is unasserted.

#### `prisma_schema_providers` moved to `app.sandboxes`, 20 Aug 2026 — done

**Decided and applied on ownership grounds, not to move the metric.** The function was defined
in `app/previews/detection.py` and had **two callers in `sandboxes`** (`engine_detection.py:101`
and `:335`) against **one in `previews`** — the private wrapper `_prisma_schema_provider`, whose
own docstring reads "for preview compatibility callers". The general parser lived in the lighter
consumer while the preview-specific wrapper wrapped it. That is inverted ownership.

The function is pure — `dict[str, bytes] -> list[tuple[str, str]]`, no preview type in its
signature, a stdlib-only body — and its docstring already said "without choosing an engine".

**It went into `app/sandboxes/engine_detection.py`, beside its two callers, not into a neutral
module.** A neutral module would create a shared bucket without a broader shared concept.
`app/previews/detection.py` now imports it back, which lands the new edge on
`previews -> sandboxes`, the preserved direction, where three preview modules already import
from `app.sandboxes.database`. The wrapper stays in `previews` unchanged.

**Cost: 2 files, +23/-22.** `re` was already imported in the destination; `PurePosixPath` was
added. Both names stay in use in the source file, so neither import was dropped there.

**Effect: `sandboxes -> previews` goes 7 sites to 6, 6 files to 5, 7 symbols to 6.**
`previews -> sandboxes` goes 16 symbols in 3 files to 17 in 4. **`KNOWN_CYCLE` does not
change and must stay `{previews, sandboxes}`** — the strongly connected component still holds
both packages; only the site count inside it shrank.

**Verified:** targeted tests 53 passed; `probe_all.py` 157 modules, 0 failed; gated suite
884 passed, 4 skipped, run twice; ungated 845 passed, 43 skipped; `ruff check` and
`ruff format --check` clean; OpenAPI byte-identical to the baseline. **Delete-test run:**
renaming the symbol at its new definition broke importing modules and restoring returned
157/0, so the new import is load-bearing rather than decorative.

**The near-miss that the brief had to name.** `app/previews/detection.py:366`, inside
`schema_environment_names`, contains a line almost identical to one in the moved function:
`if PurePosixPath(path).name != "schema.prisma":`. It belongs to a different function and was
verified untouched.

#### The extraction, measured 20 Aug 2026 — 19 files, not a pure move

**Decided: the full move. No `PreviewSettings.git_image` shim.** A shim would preserve the
wrong vocabulary and would let non-preview packages keep importing preview settings.
**`PREVIEW_GIT_IMAGE` remains the single input; `app.containers.config` becomes the single
Python authority.** Environment compatibility only.

**Four functions take a whole `PreviewSettings` and read only `git_image`.** Their parameter
becomes `git_image: str`. **19 call sites**, and the chain reaches three packages, not one:

| Function | Callers |
|---|---|
| `delivery.capture_feature_target:55` | `delivery.py:170`, `integration_review.py:182`, 8 test sites |
| `delivery.feature_diff:124` | `router.py:599`, 3 test sites |
| `feature_target.ensure_target_unchanged:49` | `publish.py:333`, **`integration_review.py:286`** |
| `publish.publish_reviewed_feature:314` | `service/publishing.py:114`, 2 test sites |

**19 files: 17 modified, 2 added.**

- **Source modified, 15.** `previews/config.py` drops the field at :21 and the env read at
  :59. Direct readers: `agents/service.py`, `previews/service.py`,
  `previews/runtimes/native.py`, `tasks/service.py`, and
  `sandboxes/service/{mirror_staleness,syncing,provisioning,transitions}.py`. Signature
  changes: `delegation/delivery.py`, `sandboxes/feature_target.py`, `sandboxes/publish.py`.
  Pass-through, import drops: `delegation/integration_review.py`, `delegation/router.py`,
  `sandboxes/service/publishing.py`.
- **Source added, 1.** `containers/config.py`.
- **Tests modified, 2.** `tests/delegation/test_delivery.py` — constructor plus 11 call
  sites. `tests/sandboxes/test_publish.py` — constructor plus 2 call sites.
- **Test added, 1.** `tests/containers/test_config.py`, covering the default and the
  `PREVIEW_GIT_IMAGE` override.

**Explicitly out of scope.** `implementation_context/config.py:9` and its `service.py` use a
different image under `TASK_GIT_IMAGE`; do not merge them.
`tests/delegation/test_docker_integration.py:31` constructs **`ContextSettings`**, not
`PreviewSettings`, and must not change. `projects/service.py` only takes `git_image` as a
parameter. The five test files that construct `PreviewSettings` without `git_image` are
covered by the default. The `publish_reviewed_feature` stubs in `tests/sandboxes/test_router.py`
take `*args, **kwargs` and need no change.

**This is not a pure move.** Four signature changes mean the AST-identity probe that
validated stages 3 to 5 does not apply. Verification has to be behavioural.

**It cuts 5 of 12 sites and leaves 7. `KNOWN_CYCLE` stays 2.** Do not expect the ratchet to
move. **That 7 is the state at this extraction, not the current figure** — the Prisma move
recorded above took it to 6.

**Method note: `grep -v "app.containers"` deletes the whole package.** The `.` is a regex
wildcard, so it matches the `/` in the *path* `app/containers/...` and silently drops every
line from files in that directory. That false negative produced a wrong "imports nothing from
any domain package" claim for both `containers` and `platform`. **Use `grep -vF`, or escape
the dot, whenever the pattern could match the path.**

**Method note: a per-file attribute grep undercounts settings consumers.** Counting
`settings.<field>` reads per file scored `git_image` at 6 sites. Three of those pass the whole
object onward — `service/provisioning.py:96` into database provisioning,
`service/transitions.py:425` into database teardown, `service/publishing.py:77` into
publishing — so the real figure is 5. `service/transitions.py` was miscounted because a
`git_image` read at line 114 sits in a different function from the pass-through at 425.
**Match whole-object pass-through as well as attribute reads.** Same trap shape as the
grep-undercount already recorded for importers and label literals.
- The other direction is 16 symbols but only 3 files, and every one is
  `app.sandboxes.database`. **Those are the figures at that scoping, not current** — see the
  table at the head of this entry.

**Nothing here is scoped or approved. Grill the scope before touching it.**

**Every edge has a module-scope import site.** There are no `TYPE_CHECKING`-only or
function-local-only edges, so no edge can be cut by re-scoping an import. Each needs the
import gone.

#### Stage 1, done 20 Aug 2026

**The cut was atomic.** `delegation` had exactly three inbound edges from the cycle, and
`implementation_context` had no inbound edge except from `delegation`. Removing any two of
the three left the SCC at 8. It could not land as three commits that each move the ratchet.

| Edge | Symbols | Cut by |
|---|---|---|
| `implementation_context -> delegation` | `DelegationStatus` | move to `controller/store/delegation_status.py` |
| `sandboxes -> delegation` | `FeatureTarget`, `ensure_target_unchanged`, `DelegationOperationError` | move to `sandboxes/feature_target.py` |
| `planning -> delegation` | `get_routing_settings` | new `agents/catalogue.py`, a real seam |

The first two are moves toward the package that owns the data. `DelegationStatus` follows
`preview_status.py` and `lifecycle_status.py`, with no re-export — all nine consumers import
from the store module. `ensure_target_unchanged` reads a sandbox volume and compares branch,
HEAD and dirty state, so it moved with the whole dirty-baseline cluster and now raises a
sandboxes-owned `FeatureTargetError`.

The third was not the 1-symbol freebie its count suggested. `get_routing_settings` returns
`RoutingSettings`, holding `ProviderModels`, whose `for_complexity` takes `Complexity` from
`delegation/models.py`. Moving the getter drags that chain. **A symbol count undercounts an
edge whenever the symbol pulls a type chain behind it.** The seam taken instead: planning
only ever asked which provider serves a model and what an operator may choose, so
`agents/catalogue.py` owns the catalogue and delegation builds on it.

**A moved refusal changes an error type, and one caller is easy to miss.** Both
`capture_feature_target` and `feature_diff` reach the moved dirty-state check. The first
attempt wrapped only `capture_feature_target`, silently changing what `feature_diff` raises.
`tests/test_delivery.py` caught it. The conversion now lives in one helper,
`_ensure_dirty_state`.

**Ruff sorts imports in this project.** An earlier handoff said the default rule set has no
isort and placement is by hand. `I` is configured here — `ruff check --fix` did the sorting.

#### Stage 2, done 20 Aug 2026

`extract_payload` was the only thing `tasks` imported from `planning`. It parses one agent
provider's stdout keyed on `AgentProvider`, so it moved to `app/agents/output.py` with its
two private helpers. That is a third misplaced-symbol edge, after the two in stage 1.

**The cost was in the error contract, not the move.** The moved code raises a new
`AgentOutputError`; `PlanningTurnError` stays in `planning/runner.py`, where three routers
import it. `run_planning_turn` converts one to the other, preserving status code, detail and
raw output, because `run_validated_turn` catches a 422 and reads `raw_output` to drive its
repair loop. Replacing the preserved status code with a literal 500 fails two tests, so the
loop is defended and the conversion is what defends it. This is the same shape as stage 1's
`_ensure_dirty_state`: **moving a refusal across a package boundary moves an error type, and
the error type is usually the load-bearing part.**

`tasks/runner.py` needed one import line changed. Both its call sites already catch bare
`Exception`.

The code changes were delegated to Codex; the verification was not. The three moved
functions and the five moved tests were checked AST-identical to their originals, because
Codex has produced correct code with a false proof before.

`tests/test_import_direction.py` guards it. Note it is a **two-way ratchet**: it fails if
the cycle grows *and* if it shrinks, telling you to tighten `KNOWN_CYCLE`. Nobody can cut an
edge without updating it. Its node set deliberately includes root-level modules — keep that,
even though `main.py` and `startup.py` are the only ones left and it catches nothing extra
today. It exists to stop the next root module from reopening the blind spot that hid a real
cycle for eight phases.

#### Stage 3, done 20 Aug 2026

Two commits, because `previews -> tasks` and `sandboxes -> tasks` had to go and **neither
alone shrinks the SCC** — `previews -> sandboxes -> tasks -> previews` still closes it.
`be81e44` moved the task status vocabulary into the controller store, cutting
`previews -> tasks`. `148d907` cut `sandboxes -> tasks`, and only then did `tasks` leave.

All three of the second commit's import sites were in `app/sandboxes/lifecycle.py`, and each
was a misplaced symbol rather than a real dependency. That is the ninth, tenth and eleventh
such edge in three stages.

| Symbol | Cut by |
|---|---|
| `LABEL_TASK_ID` | move to `app/platform/labels.py` |
| `_stop_task_preview` | move to `app/previews/service.py` as `stop_task_preview`, taking `task_id` and `sandbox_id` |
| `Task` | disappears once the helper takes ids — it only ever read `task.id` and `task.sandbox_id` |

**The move closed the last cross-package function-local import.** `tasks/service.py` imported
`stop_preview` inside `_stop_task_preview`, with a comment saying a module-scope import would
close the cycle. The comment was accurate; moving the function into `previews/` removed the
ring and the workaround together. `/tmp/localimports.py` now reports 1, down from 2, and the
survivor is intra-package.

**A brief that predicted the wrong failure was caught by Codex, not by review.** The brief for
`be81e44` said to expect the ratchet to fail. It did not, because cutting `previews -> tasks`
alone does not shrink the SCC. Codex reported the discrepancy rather than editing the test to
match. **Check your own expected-failure claims before putting them in a brief.** The brief
for `148d907` names the exact expected failure message for that reason, and it matched.

**The undefended contract this exposed.** Nothing asserts which id reaches
`controller_store.active_preview`. Passing `task_id` where `sandbox_id` belongs passes all
875 gated tests, because `tests/tasks/test_service.py` stubs `active_preview` with a lambda
that ignores its argument. Measured as pre-existing: the same corruption at `be81e44` passes
all 57 tests in that file. **Closed 20 Aug 2026** — the three stubs now assert their
argument. Note the corruption above is *one-sided*; swapping the two arguments is a
different and already-covered case. See the tripwire warning in the vocabularies entry
below.

#### Stage 4, done 20 Aug 2026

**The first edge in this series that was not a misplaced symbol.** `agents` genuinely owns
`stop_agent` and `AgentOperationError`: `replace_agent` and the agents router call the first,
two routers map the second as a domain error. Nothing could move out of `agents`, so the nine
previous cuts' method did not apply.

What worked instead was **making the odd branch look like its neighbour**. The one consumer
was a block in `drain_sandbox_writers` that called `agents.stop_agent`. Twelve lines below it,
the task branch of the same function already did its Docker work inline with a label constant
from `app/platform/labels.py`. Doing the same for the agent branch cut the edge with no new
seam, no new module and no new constant.

**The measurement that shaped the whole job: the branch had zero test coverage.** Replacing
its `stop_agent` call with a raising tripwire left all 875 gated tests green. The existing
drain test seeds `container_id=None`, so the `if container_id:` guard skipped it. With no
coverage there is nothing to break, so **"prove a preserved contract by breaking it" had no
contract to work on.** Three characterization tests landed first, in their own commit
(`7fe6ee8`), written against the old implementation. They passed **unchanged** across the
refactor, and that is the equivalence proof. They also defend the new code: changing
`timeout=2`, `force=True`, or dropping the `update_agent_run` call each fails exactly one.

**Write the characterization test before the cut, not after.** A test written after the
change can only confirm what the change already does.

Two traps that the scope found and the build avoided:

- The run id is read from the container's `orchestrator.run.id` label, **not** from the
  `agent_run_id` already in scope, and only when that label is present. An inline rewrite
  that used the variable would have looked obviously correct and changed behaviour.
- `LABEL_MANAGED` means `orchestrator.preview.managed` in `app/platform/labels.py` and
  `orchestrator.agent.managed` in `app/agents/service.py`. **Two different constants, one
  name.** Any move of the agent label vocabulary must rename, not just relocate.

**One behaviour changed on purpose, by user decision.** The managed-label guard inside
`get_managed_agent_container` is not reproduced inline. `container_id` there comes from the
controller store, not from a caller, so the check was defence in depth rather than
correctness. Keeping it would have forced the agent label vocabulary into `platform/labels.py`
— see the trap above. No test pins the dropped guard, deliberately.

#### Stage 5, done 20 Aug 2026

Two commits and two pure file moves, no signature changes anywhere.

**`aba2eeb` moved `app/projects/secrets.py` to `app/previews/secrets.py`.** Of its eight
imports, five were from `previews` and none were from `projects`. Its only consumer was
`previews/router.py`, its only test was already `tests/previews/test_secrets.py`, and its
models already lived in `previews/models.py`. The move deleted the whole
`projects -> previews` edge — 5 sites, 10 symbols — and shrank nothing on its own. **That
was the point:** it made the next cut small.

**`5a5a25b` moved `app/sandboxes/git.py` to `app/containers/git.py`**, which cut
`projects -> sandboxes` and dropped `projects` out. The file imported nothing from
`sandboxes` — only `app.containers.*` and `app.controller.config` — while six packages
imported from it. It is byte-identical in its new home.

**The lesson is about sequencing.** `projects -> sandboxes` was one symbol, `run_git`, and
had been the cheapest edge on the board for several stages. `/tmp/cycle_cuts.py` never
listed it as a cut that shrinks anything, because while `projects -> previews` existed
`projects` had two ways back into the cycle. Deleting the *other* edge first made a
one-symbol cut decisive. **A cut that shrinks nothing today can be the one that makes
tomorrow's cut trivial — read the two-edge cut list, not just the single-edge one.**

**A brief undercounted its own importer list, and Codex caught it.** The list came from
`grep -rn "app\.sandboxes\.git"`, which misses `from app.sandboxes import git` — the form
in `tests/sandboxes/test_publish.py`. Codex hit the collection error, reported it, and left
the file alone because the brief forbade touching other tests. **This is the same
grep-undercount trap already recorded for the label literals.** Match the dotted path *and*
the `from <package> import <module>` form, or the list is short.

Also worth knowing: several import blocks moved position in files that this change touched.
That is ruff sorting `app.containers` above `app.controller`, not an edit. Do not read it as
one.

### Hardcoded label literals — done, 20 Aug 2026

**The recorded figure was wrong.** The method line said
`grep -rhoE '"orchestrator\.[a-z.]+"' app`, giving 29 occurrences, 17 unique, 20 outside
`app/platform/labels.py`. That character class excludes the hyphen, so it silently dropped
every key containing one — `orchestrator.preview.data-managed`, `orchestrator.shared-database`
and eight more. **This is the grep-undercount trap of section 5, in a method line that was
copied forward unread.** Corrected with `'"orchestrator\.[a-z0-9.-]+"'`: **40 occurrences,
26 unique, 27 outside `labels.py`**, across the same 8 files.

**"20 hardcoded literals" also overstated the defect.** Read, the 27 are three different
things, and only two of the three were defects:

```text
 9  bare literal at a use site, constant already exists   fixed
 7  private alias re-spelling a labels.py value           fixed
11  a namespace one module owns, already a constant       left alone, deliberately
```

Fixed in `102e1ba`: `app/startup.py` (1), `app/sandboxes/engine_detection.py` (4),
`app/implementation_context/inventory.py` (2), `app/delegation/verification.py` (2), and the
seven private aliases in `app/platform/naming.py`. `labels.py` gained
`LABEL_LIFECYCLE_VERSION` and `LABEL_PROJECT_MIRROR`, the two keys that had no constant.

Left alone: the 8 `orchestrator.agent.*` constants in `app/agents/service.py` and the 2
`orchestrator.planning.*` in `app/planning/runner.py`. Each is a namespace exactly one module
reads, already held in a named constant block.

**`orchestrator.task.id` was on that list and should not have been.** The justification said
exactly one module reads it. Three did: `tasks/runner.py`, `sandboxes/lifecycle.py` and
`turns/locators.py`. It moved to `labels.py` in stage 3 above, where it belonged all along. `planning/runner.py` already imports the *shared* keys from `labels.py`, so
the intended pattern was working there before anything changed. Moving these would put names
in a shared module nothing else reads. **This was a user decision, not an oversight.**

**Method: verify a literal-to-constant swap with a resolved-vocabulary oracle.**
`/tmp/label_oracle.py` reads `labels.py` into a name-to-value map, then for each target file
prints every `orchestrator.*` string it reaches, resolving imported constant names to their
values. A literal becoming a constant is invisible to it; a changed *value* is not. That is
the negative control the section 5 oracle method requires. It was byte-identical across the
change apart from the two new names.

**Tripwire: the label vocabulary is close to uncovered.** Changing
`LABEL_CONTROLLER_MANAGED` to a junk value moves all five dependent sites, proving the
constants are on the call path — but it fails exactly **one** test of 835,
`tests/turns/test_events_websocket.py::test_the_context_locator_targets_the_planning_turn_container`.
834 tests do not notice a corrupted label key. **Closed 20 Aug 2026** by
`tests/test_labels.py`, which pins all 16 constants and the closed name set.

### Function-local imports — 12 hoisted or removed 20 Aug 2026, 1 remains

Method: AST walk for `Import`/`ImportFrom` nodes inside a function body — not grep.

`startup.py:98` went first, `a787fdc`. The other 11 hoistable sites followed in `283b0b1`.
Two remained, and both genuinely closed a ring. **Stage 3 removed one of them by moving its
function into `app/previews/service.py`, which dissolved the ring rather than working around
it. One remains:**

```text
app/delegation/service.py:415    view                latest_review
```

**This entry used to claim "nine of the remaining 13 sit inside the 8-node cycle and are
plausibly load-bearing". That conflated two things.** Package membership is not
load-bearing. The question is whether a module-scope path runs from the imported module
back to the importer, and for 11 of 13 no such path exists. Measured with a file-level
module-scope graph that skips `TYPE_CHECKING` blocks and function bodies, then adds the
implicit parent-package edges, because importing `app.x.y.z` executes `app.x` and `app.x.y`.
Three `__init__.py` files are non-trivial: `sandboxes/database`, `controller/store`,
`sandboxes/service`. Adding those edges moved no verdict, but not checking would have been
an assumption.

**`tests/test_import_direction.py` cannot verify a hoist.** Its graph uses `ast.walk` with
no function filter, so function-local imports already count as edges. Hoisting one adds no
edge and the test output is byte-identical either side. It was the right proof for
`startup.py`, where the negative control added a *new* edge. It proves nothing here.

**The oracle is a fresh-process import probe**: import each touched module first in its own
interpreter, then `app.main`, and both orders for every pair. 57 checks.

**Its negative control is the part that matters.** Hoist the one site already known to close
a ring — `stop_preview` into `tasks/service.py` — and the probe must fail. It does: 37
failures reading `cannot import name 'transition_task' from partially initialized module`.
The ratchet passes on that same tree. Run the control before trusting the probe.

**Two sites argued for their own deferral in comments, and both arguments were false.**

- `tasks/service.py:208` named a chain: runner → `agents.service` → `previews.service` →
  back here. `agents.service` imports `previews.config`, `previews.dependency_cache` and
  `previews.errors` at module scope. It never imports `previews.service`. The ring did not
  close at load time.
- `controller/store/projects.py:23` said the local import "makes credential-free storage an
  invariant, even when a future caller bypasses a service-layer helper". Import position
  cannot enforce a call-site rule.

**A comment that contradicts a measurement is where the measurement is usually wrong.** Here
it was the comments. Trace the chain rather than accepting either side.

**A hoist rebinds the name, so monkeypatch targets move.** The first gated run failed 9
tests in `tests/tasks/test_service.py`: `app.tasks.runner.run_coding_turn` no longer reached
the live binding. `run_coding_turn` has one call site, so the retarget to
`app.tasks.service.run_coding_turn` was unambiguous. **Those 9 failures are the negative
control for the old target** — a hoist that breaks no test either has no patch aimed at it
or has one aimed somewhere that never mattered.

**`transitions.py:259` was the free one.** `app.sandboxes.mirror` was already imported at
module scope in that file, five names at line 35. `MirrorPin` joined the block, so no new
module-load order existed at all.

Left alone: `tasks/service.py` still guards `from app.tasks.runner import CodingTurnResult`
behind `TYPE_CHECKING`, above an unconditional import of the same module. Harmless and now
pointless, but it is not function-local, so it was outside this item.

### `PreviewStatus` — done, 20 Aug 2026

`189a840`. The analysis this entry demanded changed the shape of the job. The entry read
"preview status is still strings", which frames it as typing. The measured problem was
duplication across two languages.

**The vocabulary is nine values; eight are written.** Active: `preparing`, `running`,
`restarting`, `rebuilding`, `stopping`. Terminal: `stopped`, `failed`, `missing`, `expired`.
Nothing writes `rebuilding`.

**The active set was spelled four times**, at `controller/store/previews.py:14` and `:20`,
in the `preview_runs` branch of the UNION at `controller/store/agents.py:187`, and in the
`one_active_preview_per_sandbox` UNIQUE INDEX at `controller/store/schema.py:67`.

**That index is the reason the item was worth doing.** It enforces one active preview per
sandbox. Add an active status to the Python tuples and miss the index, and two active
previews can coexist. **A Python enum cannot reach the index** — SQLite bakes the literals
into the definition — so an enum alone would have unified two of four sites and left the
load-bearing pair drifting. The IN-list is now generated from the same tuple.

`app/controller/store/preview_status.py` owns `PreviewStatus`, `ACTIVE_PREVIEW_STATUSES`,
`TERMINAL_PREVIEW_STATUSES` and `ACTIVE_PREVIEW_STATUS_SQL`. `previews/models.py` imports
from it, not the reverse: `controller` is in `MODULES_OUTSIDE_KNOWN_CYCLE` and stays there.

**Three constraints found by measuring, each of which would have been a defect:**

- **`PreviewContainer.status` is not ours.** It holds Docker's container status —
  `created`, `paused`, `exited`. Typing it with `PreviewStatus` would be a bug. Untouched.
- **`ACTIVE_PREVIEW_STATUSES` is an ordered tuple, not a frozenset.** Byte-identity with the
  index in existing databases depends on the value order.
- **Keeping `rebuilding` forces it into the enum.** Generating the clause from a tuple that
  omitted it would silently drop it from the index and move the schema.

**`rebuilding` is out of the active set, `d0f0d07`, but stays in the enum.** Migration 32,
`_remove_rebuilding_from_active_previews`, follows the migration 23 precedent. It builds its
IN-list from `ACTIVE_PREVIEW_STATUS_SQL` rather than hardcoding literals; migration 23
hardcodes its own list only because it predates that constant.

**The index and the enum wanted opposite answers, and this entry's reasoning for keeping it
was wrong.** The entry said `rebuilding` is harmless "because no row can ever hold it".
That is true of *this* code, not of whatever wrote existing databases. `PreviewRun.status`
and `PreviewLogs.status` are typed `PreviewStatus`; a probe confirms Pydantic raises a
`ValidationError` of type `enum` on a row holding a value the enum no longer lists. So
dropping it from the index is safe on any database and dropping it from the *enum* breaks
reads of any legacy row. The member stays for reads, and its comment now says so.

**Narrowing a partial UNIQUE index cannot fail on existing data.** It only ever shrinks the
indexed row set, so it can never introduce a duplicate violation. No backfill is needed.

**The write sites are done, `4fe0e37`, and the count recorded here was wrong.** This entry
said 16. There are 20. The four it missed are a dict-literal insert at
`previews/service.py:303`, the `stop_preview` status default at `:710`, and the two result
branches of the ternary at `startup.py:195` and `:197`. The first two were missed because
the grep looked for `status="` and these spell it `"status":` and `status: str =`. The last
two were missed because the entry classified `startup.py` down to one site and stopped
reading at that line, inside the same loop.

**That ternary is the sharpest trap in the file.** Its condition
(`item.status == "running"`) is Docker's vocabulary and its two result branches are ours —
one statement, two vocabularies, same spelling. A blind replace corrupts the condition and
nothing catches it.

**The frontend constrains nothing.** Five sites render `status` as text and none branch on
a value, so no frontend change was needed. `ContainerStatusBadge.tsx` reads Docker's
vocabulary, not this one.

**Method: two oracles, and the schema one is the real gate.** A 54-object dump of
`sqlite_master` from a freshly initialised store must be byte-identical across the change —
`/tmp/schema_oracle.py`. It was. The OpenAPI dump is expected to differ and did, gaining a
`PreviewStatus` enum on exactly the two typed fields. **Tripwire:** adding a value to the
tuple moves both the index and the UNION, and leaves the `tasks` branch of the same UNION
alone. That last part is the negative control — without it the tripwire cannot tell
generation from over-generation.

**Correction, 20 Aug 2026: the schema oracle is not a gate for migration work.** It reads a
*freshly initialised* store, so it never exercises a migration path. Measured while landing
migration 32: with migration 32 deleted from the `MIGRATIONS` map, the oracle's output is
byte-identical to the correct run, because `schema.py` generates the narrowed index either
way. A missing migration is invisible to it, and so is a wrong one on any database that
already exists. Use a forward-migration probe instead — see the entry below.

### Migrations need a forward probe, not a schema dump — method, 20 Aug 2026

Landing migration 32 produced a probe worth reusing, and one wrong turn worth not repeating.

**The wrong turn: comparing a fresh database against a migrated one is vacuous.** Migrations
run on fresh databases too, so migration 32's `DROP INDEX`/`CREATE` overwrites what
`schema.py` just built. Both sides of that comparison execute the same code and agree even
when the code is wrong — a deliberately corrupted migration body produced two identical
*wrong* indexes and the probe reported success. Codex's own verification made the same
mistake and reported the same false pass.

**What works,** `/tmp/migration32_probe.py`: initialise a database, force it back to the
pre-migration state (restore the old index, delete the version stamp), re-initialise, then
compare the result against an **expected string written out by hand in the probe**. The
independent reference is the whole point.

**Two negative controls, and they catch different failures:**

| Control | Caught by |
|---|---|
| Migration body spells the status list wrong | the probe, and the new index test |
| Migration left out of the `MIGRATIONS` map | the applied-version assertions |

Neither is caught by the schema oracle. The corrupt-body case was caught by *nothing* until
`test_the_active_preview_index_holds_exactly_the_active_statuses` was added — it works
because migration 32 runs on fresh databases, so a corrupt body lands in a fresh store's
index where a test can see it.

**The applied-version assertions are a real ratchet.** Ten of them, across three test files,
list every migration version explicitly. Adding a migration fails all ten until you add the
number. That is the mechanism forcing you to notice; do not weaken it to a length check.

### The undefended vocabularies — decided and partly closed, 20 Aug 2026

The label tripwire and the preview-status tripwire found the same thing in two different
vocabularies. Both are pre-existing. Neither was caused by the change that found it.

**Preview status, measured at `4fe0e37`.** Corrupt one `PreviewStatus` member's value and
run the gated suite. Only two of eight members are defended:

| Member | Gated tests that fail |
|---|---|
| `RUNNING` | 8 |
| `PREPARING` | 5 |
| `RESTARTING` | 0 |
| `STOPPING` | 0 |
| `STOPPED` | 0 |
| `FAILED` | 0 |
| `MISSING` | 0 |
| `EXPIRED` | 0 |

**The obvious explanation is wrong, and the wrong one was written down first.** The first
reading was that `RUNNING` is defended because it sits in the
`one_active_preview_per_sandbox` index and `FAILED` is not because it is terminal. But
`RESTARTING` and `STOPPING` are *also* in that index — corrupting either rewrites the index
SQL — and both score zero. **Being load-bearing in SQL defends nothing.** What defends
`PREPARING` and `RUNNING` is that a preview passes through them on the happy path, so
functional tests observe them in passing. Every status reachable only on a teardown,
failure, or expiry path is unasserted.

So the defences split three ways, and only the first is a test:

- **Tests catch it:** `PREPARING`, `RUNNING`.
- **Only the schema oracle catches it:** `RESTARTING`, `STOPPING`, and `REBUILDING`. The
  oracle is a `/tmp` script, not a test, so it fires only when someone remembers to run it.
- **Nothing catches it:** `STOPPED`, `FAILED`, `MISSING`, `EXPIRED`.

**Labels, measured the same day.** A junk `LABEL_CONTROLLER_MANAGED` fails exactly one test
of 835. Details in the label entry above.

**The sweep needs its own guard, and that guard is the finding's credibility.**
`/tmp/status_coverage.sh` greps for the junk value after each `sed` and records
`SED-FAILED` if it did not match. Without it a `sed` that matched nothing yields an
unmodified file, a green run, and a result indistinguishable from a real coverage gap.
`RUNNING` doubles as the positive control: it was confirmed at 8 failures by hand first, so
a sweep reporting zero for `RUNNING` is a broken sweep, not a coverage gap.

#### Decided, 20 Aug 2026 — and the framing above was wrong

The paragraph this replaces said it was **one decision, not two**, and that one mechanism
should cover both vocabularies. That is what kept it open across four sessions. There are
**three** cases, not two, and they are **three different failure modes**. No single
mechanism covers them:

| Mode | Example | What catches it |
|---|---|---|
| Value corruption | `LABEL_MANAGED` edited to junk | A contract test. Fully. |
| Wrong member used | code writes `STOPPED` where `EXPIRED` belongs | Only a test driving that path |
| Wrong value passed | `active_preview(task_id)` | Only a test at the calling path |

**The policy, by user decision:**

- Pin closed vocabularies with explicit contract tests.
- Test important argument selection at the calling path.
- Test status transitions through their behaviour.
- **Do not claim one mechanism covers all three failure modes.**

**What landed.** `tests/test_labels.py` pins all 16 `LABEL_*` constants;
`tests/controller/test_preview_status.py` pins all 9 `PreviewStatus` members. Both assert an
explicit literal name-to-value map **and** that the set is closed, so a new constant fails
until it is listed. The three `active_preview` stubs in `tests/tasks/test_service.py` now
assert their argument. Gated suite 878 -> 882.

**Rejected, with reasons.** A runtime guard on `active_preview` was proposed and rejected:
`task_id` is `uuid4().hex` and `sandbox_id` is `uuid5(...).hex`
(`app/platform/naming.py:41`), so **both are 32-character hex and no shape check can
separate them**. The guard would have rejected valid v1 sandbox ids.
`app/projects/service.py:71` reads like a mint site and is not — it passes an
already-resolved lookup key through under another name. Promoting `/tmp/schema_oracle.py`
and `/tmp/label_oracle.py` into the repo was also rejected: they print diagnostics and
contain no assertions. That is a separate piece of work, and it is **not** the same as
open item 4, which names `probe_all.py` and `migration32_probe.py`.

**Still open:** behavioural path coverage for the four statuses nothing catches — `STOPPED`,
`FAILED`, `MISSING`, `EXPIRED`. Those are teardown, failure and expiry paths, so the tests
are Docker-gated. Deferred on purpose. It is path coverage, not vocabulary protection, and
it must be scoped separately rather than smuggled in beside cheap work.

#### The tripwire tested the wrong corruption — read this before reusing it

The obvious tripwire for the `active_preview` contract is to **swap** the two adjacent
positional arguments at `app/tasks/service.py:564`. **That swap was never invisible.** It
makes `active["task_id"] != task_id` inside `stop_task_preview`, so the function returns
early at `app/previews/service.py:772`, `stop_preview` never runs, and the existing
`assert stopped == [...]` catches it. Measured against the **old** stubs:

| Corruption at the accept call site | Old stubs | New stubs |
|---|---|---|
| Swap both arguments | **2 of 3 already fail** | 3 fail |
| One-sided: `sandbox_id` param receives `task.id` | **57 passed** | 3 fail |

The invisible corruption is the **one-sided** one — the case this entry described from the
start. A swap changes two things and the second change betrays the first. **A tripwire that
corrupts two coupled arguments at once tests a corruption the suite already caught, and
reports the guard as effective for the wrong reason.** Corrupt one argument, not the pair.

**The negative control is what found this.** Running the tripwire against the *new* tests
proved only that the new assertion fires. Running the same corruption against the *old*
tests is what showed the swap was already covered — and it is the only step that
distinguishes a test that closes a gap from a test that restates existing coverage.

---

## 3. Cleared, 20 Aug 2026

Two rounds. The first five items landed on `chore/open-work-section-3`. Two more landed on
`chore/label-constants` later the same day: the label literals of section 2 and the inert
patch sites of section 1. Kept here because four of the seven returned findings worth
reading, not to claim credit.

**Both of the later two had a wrong figure recorded against them, and in both cases the
error was already written down beside the number.** Read the caveat before trusting the
count it qualifies.

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
  passes in 0.82s without it. **The Docker-down path it claimed to cover is covered
  elsewhere**, in `tests/test_startup.py` — see the disproved entry in section 1. The
  rename was still right; the gap it seemed to expose was not one.
- **`tests/controller/test_lifecycle.py`** moved to `tests/test_startup.py`. It draws its
  helpers from `tests/conftest.py`, so the depth change needed no other edit.
- **The frontend layering oddity** is fixed. `PhaseAgent` moved out of
  `pages/planningSessionModel.ts` into `components/planningAgentInspectorModel.ts`,
  matching the existing `components/delegationWorkspaceModel.ts` convention.
- **`ruff` is installed and green.** See below; it was not a small item.

### The inert patch sites, measured and closed

`ac144d6`. Section 1 recorded **ten** inert patch sites. Measured gated, the real number is
**one dead patch and two blind tests**, and the two figures are answers to different
questions.

```text
site                                                     delete patch   neuter value
tests/delegation/test_delivery.py fetch_canonical_mirror  FAILS          -
tests/delegation/test_delivery.py mirror_base_commit      FAILS          -
tests/delegation/test_delivery.py sync_workspace_from_..  FAILS          -
tests/delegation/test_delivery.py complete_database_pro.  passes         FAILS
tests/delegation/test_delivery.py discover_engine         passes         never called
tests/sandboxes/test_sync.py      complete_database_pro.  passes  x5     FAILS 3 of 5
```

Three of the five `test_delivery.py` sites bite outright. They read inert only because the
harness ran ungated and the whole test was skipped.

**`discover_engine` was genuinely dead** — replaced with a function that raises, the test
still passes. A dead patch can also mean a patch aimed at the wrong module, the "one site
becomes two" shape that bit the database split three times, so
`app.sandboxes.service.transitions.discover_engine` was probed the same way. Also never
called. Deleted, and `EngineDetection` dropped from the import with it.

**Two of the five `test_sync.py` tests were blind.** All five reach
`complete_database_provision`, and its replacement `_complete_sync` carries real assertions
about the engine-detection snapshot. But only three tests noticed when the replacement was
gutted, so two would not have seen `sync` drop the call entirely. A `_recording_complete_sync`
wrapper and one assertion per test closes it. `_complete_sync`'s body is unchanged; its
assertions are the coverage.

`test_sync_merges_only_after_an_observed_open_pull_request` syncs twice, so it clears the
recorder beside the `calls.clear()` it already had. **The delegate caught that and stopped
rather than working around it** — the scope said one entry and the truth was two.

**Tripwire.** Silencing the recorder fails all five tests. Before the change, gutting the
substitute failed three. That difference is the whole deliverable.

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

Decided in `docs/adr/0009-two-shared-database-implementations.md`. That ADR still cites
the pre-package path `app/sandboxes/database.py` at four line numbers, all now wrong. It is
left as written, because an ADR records a decision at a date. The current locations are
below. Keep both
`previews/sharing.py:133 _shared_database_server` and
`sandboxes/database/__init__.py:1239 _ensure_shared_server`. Change no code.

They are not duplication. `sandbox_database_runtime` (`sandboxes/database/__init__.py:893`)
routes a preview to the previews path whenever the sandbox has
`lifecycle_version != "v1"` or `db_engine == "none"`, and to the sandboxes path
otherwise. Previews is MySQL-only by construction, so it cannot address a PostgreSQL
shared server at all.

Two of the five behavioural differences the old entry listed are not behavioural.
`database=` is unread when `shared=True` (`sandboxes/database/__init__.py:187`), and container
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
- **A green suite is not proof a test covers anything.** Thirteen instances so far. One
  phase ran a green 834-test suite with the project's real `.env` file silently not loading.
  `app/controller/store/events.py` held a latent `NameError` in its module annotations across
  eleven phases, because no test ever read them. The newest instance inverts the rule: a
  *skipped* suite proves even less. Six gated tests rotted unnoticed over four handoffs
  because the gate hid them, and one of the six is green on any first run and red on the
  second.
- **An oracle that runs the changed code on both sides proves nothing.** The migration 32
  probe compared a fresh database against a migrated one, not realising migrations also run
  on fresh databases. A corrupted migration produced two identical wrong answers and the
  probe passed. Before trusting any before/after comparison, ask which side is *independent*
  of the change. If neither is, write the expected value out by hand.
- **Run `ruff format --check` after any mechanical rename.** `4fe0e37` swapped short string
  literals for longer enum members and pushed four lines over the limit. The full gated
  suite stayed green; only the formatter noticed.
- **When a tripwire fires on some inputs and not others, test your explanation of why.** The
  preview-status sweep looked like "the index defends it" after two data points, and that
  reading was reported before the other six ran. It was wrong: two more members sit in the
  same index and nothing notices when they change. Two data points suggest a mechanism; they
  do not establish one. Sweep the whole vocabulary before naming the cause.
- **A contract change moves logic into a translator, and nothing tests a translator.**
  Twice now, one layer apart. Extracting `PreviewRuntimeLimits` left the composition between
  the settings factory and the new object unasserted; narrowing `DatabaseConnectionRequest`
  left the preview-to-sandbox translation unasserted. In both cases the tests on each side of
  the seam were thorough and the seam itself had none, because each suite constructs the
  object it owns and never exercises the hand-off. **When a change makes one type stop
  carrying another, write the test that runs the conversion**, then tripwire it to prove that
  test is the one that fires.
- **Verify a monkeypatch retarget by DELETING the patch**, not by aiming it at a module that
  binds no such name — the latter raises from `monkeypatch.setattr` itself and so fails for
  the wrong reason. If the test still passes without the patch, check whether it is green for
  an environmental reason before concluding the patch is cosmetic. Stronger still, when the
  patch moved off the package facade: revert it *to the facade* and confirm the test fails.
  That proves the new target does work, not merely that it holds a real attribute.
- **Enumerate patch targets from the call graph, not the caller's body.** Step 2 of the
  database split (`d1216a3`) inherited a note saying one patch was dead because
  `_ensure_shared_server` never calls the helper. It reaches it through
  `database_engine(...).provision(...)`. The same split turned one patch site into two,
  because a method and a helper it calls came to live in different modules. Patch every
  module that binds the name *and* sits on the call path. The cheapest reliable check is
  `f.__globals__ is target.__dict__` — a function's globals dict *is* the namespace its
  names resolve in, so it settles the question without running the test.
- **When a reverted patch still passes, install a tripwire before believing it.** Step 3
  (`0366c17`) reverted `_wait_for_server_health` to the package facade and the test stayed
  green — the real health wait just succeeds harmlessly against the fake Docker client.
  Replacing the real binding with a recorder proved it was reached once before the retarget
  and zero times after. The same tripwire caught a retarget the delegate had silently
  skipped, four of five done, suite green either way. A green suite is not coverage.
- **A guard patch cannot be proved by reverting it.** A patch whose value raises on call
  asserts that a path is never taken, so the test passes with it deleted. Prove those by
  `__globals__` identity, never by deletion.
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
- **Run the Docker-gated suite twice in a row, and treat only the second run as the
  result.** `RUN_DOCKER_PREVIEW_TESTS=1`, from `backend/`. One green run cannot tell a
  repeatable suite from one that poisons its own next run. That is exactly how a sixth
  rotted test survived eleven phases.
- **Delegated work is unverified until you run what the delegate could not.** Codex cannot
  reach the Docker socket here, so its green ungated run skipped every test it was fixing.
  Its first report claimed the volumes met v1 ownership validation, which it had no way to
  observe. Three rounds were needed, and each round's real diagnosis came from a run on this
  side. Give the delegate the measured error, not the plan's description of it.
- **A lint rule that fires 20-plus times is usually one pattern, not 20 defects.** Read a
  handful before fixing any. Of ruff's 353, the 34 `ISC004` were all deliberate multi-line
  prompt strings and the 23 `F811` were all one pytest fixture idiom. Two of the 353 were
  real defects: one `F821` and one `F811`, both single occurrences, both dead code left by
  an earlier split phase. The value of the gate was in those two, not in the other 351.
- **Verify a mechanical string rewrite with a string-constant oracle.** Count every `str`
  constant per file before and after and diff. The suite cannot tell you that 34 rewrapped
  prompts are byte-identical. Three files differed and each had to be accounted for by
  hand.
- **Never mix a file move with a behaviour change** in one commit. On the `database.py`
  step-1 split, Codex appended ten `SomeClass.__module__ = __name__` lines so signature
  reprs would keep printing the old module path. Nothing needed them; 835 tests passed
  either way. They were removed.
- **A delegate will change behaviour to make your probe pass.** So normalise the probe
  before you trust it — the first version of the step-1 probe printed raw memory
  addresses, which gave Codex noise to "fix". Then diff the produced files for anything the
  prompt never asked for, and delete-test each such line.
- **Prove a pure move by comparing definitions, not output.** Parse the old file and the
  new tree, and diff every top-level definition by `ast.dump`. Step 1 compared 67 of them:
  none missing, none added, no body changed, none duplicated across modules. Unlike a repr
  or surface probe, this cannot be satisfied by a cosmetic tweak, so make it the primary
  check and the probe the secondary one.
- **When removing or moving code, let the tests break loudly.** Repoint the test at the
  module that now binds the name. A shim, alias or re-export is the wrong fix. The one
  standing exception is a package `__init__.py` acting as the public surface for a module
  split into a package — `app/controller/store/` and `app/sandboxes/database/`. That is a
  facade, not a shim left behind at an old path.
- **Enumerate monkeypatch sites with an AST pass, never with grep.** `monkeypatch.setattr(`
  often puts the module on one line and the name on the next. A grep count during the
  `service.py` scoping said 10 names across 16 sites; the AST pass found **15 names across
  50 sites**, and the wrong figure reached the scope doc before it was caught.
- **Re-measure line ranges between every step of a multi-step split.** Step 1 shifts every
  number in the file. Ranges measured before step 1 cut in the wrong places at step 2.
- **A submodule must never share a name with a function it exports.** The facade's closing
  `globals().pop(<submodule>)` block deletes the function instead. A module `staleness.py`
  holding a function `staleness` lost the function this way. Check every new submodule name
  against its exports before writing the pop block.
- **Prune a package facade in the same session as the split, not later.** With every
  patched name absent from the facade, a stale patch raises `AttributeError`. Pruning late
  turns loud failures into findings you have to go looking for.
- **Read background-command output; never trust the exit code.** One run reported exit 0
  while every command inside had failed — the trailing `echo` succeeded. Background commands
  do not inherit the shell's working directory, so use an absolute `cd` inside the command.
- **A caveat written beside a figure does not correct the figure.** Twice on 20 Aug 2026
  this doc recorded a number and, in the same block, the reason that number was wrong: the
  "ten inert patch sites" came from an ungated run whose gated-tests-are-skipped trap was
  written two paragraphs below, and the "20 label literals" came from a regex whose
  character class the doc printed in full. Both survived because a caveat reads like
  diligence. **When you write down why a measurement might be wrong, re-measure or mark the
  figure unverified — do not record it as a finding.**
- **A method line copied forward is not a method.** The label-literal grep was carried
  across several handoffs without anyone running it against the question it claimed to
  answer. Re-run the method, do not re-read it.
- **"The test passes without the patch" and "the test notices if the patch does nothing"
  are two different questions.** The first only tells you the real function is harmless in
  that fixture. The second finds the weak test. Run both and read them apart; where they
  disagree is the gap.
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

---

## 6. Environment and delegation

**Running anything.** Work from `backend/`, using `backend/.venv/bin/python` and
`backend/.venv/bin/ruff`. The system python has no `app` on its path.

- Ungated: `.venv/bin/python -m pytest -q` → **854 passed, 43 skipped**, ~27s.
- Gated: `RUN_DOCKER_PREVIEW_TESTS=1` from `backend/` → **893 passed, 4 skipped**, ~174s.
  Run it twice and read the second run; see section 5.
- **`AGENTS.md` section 11 now documents all four `RUN_DOCKER_*` gates**, what each unlocks
  and which three also need a real model and therefore cost money. It previously named none
  of them while telling the reader to use "the documented opt-in command". Recorded 21 Aug 2026.
- Lint: `ruff check app tests`, then `ruff format --check` (252 files).
- Frontend gate: `npm run build`, not `tsc --noEmit`.

**Colima.** Must be running for the gated suite. On a stale disk lock
(`failed to run attach disk "colima", in use by instance "colima"`):
`colima stop --force`, then `colima start`.

**Docker leak.** Anonymous, hash-named volumes accumulate across gated runs, 6 to 8 per run.
They are empty and reclaim 0B, so this is clutter, not a disk problem. Prune only by the
exact prefix `orchestrator-preview-` or `orchestrator-deps-`. **Never
`--filter name=orchestrator`** — it also matches
`orchestrator-agent-auth-<provider>-<profile>-<digest>` and deletes agent CLI logins.

**Delegation.** The standing split is: Codex implements, this side verifies. It has held
across six mechanical steps with no rework.

- Invoke Codex pinned, and close stdin or it hangs forever:
  `codex exec -m gpt-5.6-terra -c model_reasoning_effort="high" -s workspace-write "<prompt>" < /dev/null`
- Terra at high effort was enough for every step so far.
- Include the line **"if you conclude an instruction of mine is wrong, STOP and say so"**.
  It is the highest-value line in the prompt. Codex stopped twice on the `service.py` split
  and was right both times; both were errors in the prompt.
- Give it exact current line ranges, per-module import lists, target layering, an explicit
  "pure move, change nothing" rule list, and a **Forbidden** section.
- Write verification commands relative to the directory Codex runs them from — the repo
  root, where `app/` and `tests/` do not exist.
- Codex **cannot** reach the Docker socket here and cannot take screenshots. Its `.git` is
  read-only: create the branch first, and commit on this side.
