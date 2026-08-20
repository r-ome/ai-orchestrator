# Open work

**State:** `refactor/cycle-stage-3` @ `148d907`, 20 Aug 2026. Two commits ahead of
`origin/main` (`afec862`), not pushed.
**Suites:** backend 836 passed, 43 skipped, ~30s. Gated backend 875 passed, 4 skipped, ~140s,
run twice. Frontend 80 passed, `npm run build` clean — **not re-run since `189a840`; no
frontend file has changed since.**
**Lint:** `ruff check app tests` passes. `ruff format --check` reports 240 files formatted.

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
returned a finding. **What is still open is the 5-node import cycle, and the 2
function-local imports of the original 14 that genuinely carry it.** One more thing is open
and undecided: **two vocabularies — Docker labels and preview status — are each close to
undefended by tests**, which is now its own entry.

Done 20 Aug 2026: the preview-status write sites (`4fe0e37`), the dead `rebuilding` status
and migration 32 (`d0f0d07`), the `startup.py` import hoist (`a787fdc`), the remaining
11 hoistable function-local imports, stage 1 of the cycle (`6e5a30b`) and stage 2 (`ba65624`).

### The domain import cycle — 4 nodes, was 8

`agents, previews, projects, sandboxes`. **9 intra-cycle edges**, was 29.

**The "15 intra-cycle edges" recorded here for the 5-node graph was wrong; it was 14.**
Re-measured with `/tmp/cycle_edges.py`. Assume the next figure on this page is wrong too.

Phase 9 cut it from 10 nodes to 8. Stage 1 (`6e5a30b`, 20 Aug 2026) cut it to 6 by removing
`delegation` and `implementation_context`. Stage 2 (`ba65624`) cut it to 5 by removing
`planning`. Stage 3 (`be81e44` and `148d907`) cut it to 4 by removing `tasks`. See below for
what each took.

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
original 8-node graph was **8 edges, 25 symbols, 19 files**. Stages 1 to 3 removed 20 of the
29 intra-cycle edges. Which of those were members of that original minimum set has not been
re-derived, so do not quote a "spent" count — re-measure the current graph instead.

**What remains is not what this page predicted, and the difference is a cheap cut nobody
had scoped.** Re-measured on the 4-node graph with `/tmp/cycle_cuts.py`:

- **`sandboxes -> agents` is a single-edge cut that takes it 4 nodes -> 3.** It is **2
  symbols in 1 file**: `AgentOperationError` and `stop_agent`, imported by
  `app/sandboxes/lifecycle.py:14`. Only `projects -> sandboxes` is cheaper, at 1 symbol, and
  cutting that one shrinks nothing. Cutting this one drops `agents` out. **Not scoped, not
  approved, and the seam is not obvious — a moved refusal moves an error type, and that has
  been the load-bearing part every time so far.**
- **The objection recorded against this edge no longer applies.** The stage-2 handoff ruled
  it out because the route to 4 nodes needed `tasks -> agents` cut alongside it, and that
  meant moving `extract_payload` back out of `agents/`, undoing `ba65624`. Stage 3 took
  `tasks` out of the cycle altogether, so `sandboxes -> agents` now stands alone and costs
  nothing from `ba65624`. That ruling was never written into section 4, and it is now spent.
- After that the remainder is `previews <-> projects`: **17 symbols across two directions**,
  `projects -> previews` 10 and `previews -> projects` 7, and no cheap cut. The minimum
  feedback arc set for the whole current graph is **3 edges, 16 symbols, 14 files**.

**Grill the scope before touching either.**

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
all 57 tests in that file. It belongs with the two undefended vocabularies below; nobody has
decided to close it.

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
834 tests do not notice a corrupted label key. Nobody has decided to close that.

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

### Two vocabularies are close to undefended by tests — open, measured 20 Aug 2026

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

**Undecided, and it is one decision, not two.** Both vocabularies pose the same question:
what should assert a vocabulary? A snapshot test is the cheap answer for both and catches an
accidental edit; it catches neither a container labelled with the wrong constant nor a
status written through the wrong member. **Ask before choosing, and decide both together** —
answering them separately will produce two different mechanisms for one problem.

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

- Ungated: `.venv/bin/python -m pytest -q` → **835 passed, 43 skipped**, ~31s.
- Gated: `RUN_DOCKER_PREVIEW_TESTS=1` from `backend/` → **875 passed, 4 skipped**, ~140s.
  Run it twice and read the second run; see section 5.
- Lint: `ruff check app tests`, then `ruff format --check` (239 files).
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
