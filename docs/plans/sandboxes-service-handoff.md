# Handoff — Candidate #3: give `app/sandboxes` a service module

**Repo:** `/Users/jeromeagapay/orchestrator_v2`
**Supersedes:** `/var/folders/81/.../handoff-sandboxes-service-module.md` (18 Aug 2026)
**Re-verified:** 18 Aug 2026, against branch `refactor/hardened-boundary` @ `2776b74` + uncommitted work

Read this document instead of the original. It carries the original's still-true
content and corrects what drifted. Corrections are marked **CORRECTION**.

---

## 1. Start here: #2 is not done

**CORRECTION.** The original handoff assumed #2 might have landed. It has not.

`refactor/hardened-boundary` is the #2 branch, and it is *this* working directory.
State at time of writing:

| Fact | Value |
|---|---|
| Branch | `refactor/hardened-boundary` |
| Head | `2776b74` refactor(containers): widen the hardened boundary to every container |
| Uncommitted | 9 files, +222 / −207 |
| Merged to `main`? | No. `main` is still `fc274f4`. |

**Do not start #3 until this branch is committed and merged, or until the user says
to branch anyway.** The uncommitted work is the blocker, not the commit.

### #2 is stacked on #1

**CORRECTION.** The original said "Branch from `main`, not from
`refactor/validated-turn` — #1 is unrelated and still unmerged."

That is no longer possible to follow cleanly. `refactor/hardened-boundary` contains
`refactor/validated-turn`:

```
2776b74  #2  refactor(containers): widen the hardened boundary to every container
65668b4  #1  refactor(turns): run every validated turn through one module
a9588a9  #1  refactor(delegation): name the verification repair prompt for what it is
fc274f4      main
```

So #1 and #2 land together or not at all. `git diff --stat main..HEAD` is 19 files,
+818 / −448 — that is both candidates combined.

**Ask the user which base #3 takes.** Two options, no default is obviously right:

- **Branch from `main`** — keeps #3 independent, but #3 then cannot see #1 or #2, and
  merges after both.
- **Branch from `refactor/hardened-boundary`** — #3 builds on current reality, but
  inherits two unmerged candidates and cannot merge before them.

### #2's real scope is not what was predicted

**CORRECTION.** The original predicted #2 touches `previews/service.py` and
`sandboxes/database.py`. **It touched neither.** Actual footprint:

```
committed (main..HEAD):   containers/hardened.py, containers/images.py,
                          delegation/{execution,integration_review,prompts,service,verification}.py,
                          implementation_context/{prompts,service}.py,
                          planning/{runner,service}.py, tasks/runner.py, CONTEXT.md
uncommitted:              agents/service.py, controller/config.py,
                          implementation_context/inventory.py,
                          sandboxes/engine_detection.py, sandboxes/git.py
```

**Consequence for #3: the predicted conflict is gone.** #2 never touched
`sandboxes/router.py` or `sandboxes/database.py`. The original's "overlap risk is low
but real" warning about `database.py` signatures does not apply.

**One new, smaller overlap.** #2 has uncommitted edits in `sandboxes/engine_detection.py`
(−28 net) and `sandboxes/git.py` (+4 net). Neither is in #3's five-handler scope. The
nearest contact is the `get_engine_detection` / `confirm_engine` handlers
(`router.py:1013`, `:1026`) and `_confirm_engine_snapshot` (`router.py:1566`) — all
outside the scope recommended below. Risk is real but small; do not extend #3 into the
engine-detection handlers while #2 is live.

---

## 2. The problem, unchanged

`app/sandboxes/` is the only package with no `service.py`. Every neighbour has one
(`previews/`, `projects/`, `tasks/`, `planning/`, `delegation/`, `agents/`,
`implementation_context/`). The workflows live in the route handlers instead, so they
are reachable only through HTTP and testable only through a test client.

---

## 3. Evidence — re-verified against the current tree

### Handler sizes: all confirmed exactly

`app/sandboxes/router.py` is still 1753 lines. Every line range in the original still
lands on the function it claimed:

```
 271- 441   170 lines  create_or_resolve_sandbox
 512- 589    77 lines  get_sandbox_staleness
 590- 780   190 lines  sync_sandbox
 781- 999   218 lines  publish_sandbox
1135-1291   156 lines  resume_sandbox
1602-1739   137 lines  _complete_database_provision   <- service function in the wrong file
                       ------
                       948 lines across 5 handlers + 1 helper
```

### Leaf modules

```
1629  database.py      862  git.py *        477  engine_detection.py *
 401  publish.py       260  lifecycle.py    176  naming.py
 146  mirror.py         98  orphans.py       62  manifest.py
```

`*` = has uncommitted #2 edits. Note these two counts are the *working tree*; at
`main` they are 858 and 505.

**CORRECTION, minor but instructive.** The original claimed all its figures were
"checked directly against `fc274f4`". Its leaf figures (862, 477) are working-tree
values, not `fc274f4` values (858, 505). The original was written against this dirty
tree. The discrepancy changes nothing for #3, but it confirms the original's own
warning: re-check numbers before acting on them.

---

## 4. The lock-ordering invariant — materially revised

This is the load-bearing part of #3, and the original got the *location* wrong in a way
that makes the work easier, not harder.

### There are four prose statements, not three

```
app/controller/store.py:694     "callers must acquire a sandbox lease first and this project lock second"
app/controller/store.py:3828    "Lock order for callers that need both locks is: sandbox lease first, then this project mirror lock"
app/sandboxes/router.py:826     "Lock order is fixed: sandbox lifecycle lease, then project mirror lock"
app/sandboxes/lifecycle.py:105  "Callers holding both locks must enter lifecycle_lease first and this context second"   <- MISSED
```

Nothing detects a violation. Four statements, zero enforcement.

### CORRECTION: enforcement does not reach into `controller/store.py`

The original warned that enforcing lock order "reaches into `controller/store.py`,
which candidate #6 also touches — check with the user."

**That warning is wrong and can be dropped.** Both context managers already live in
`app/sandboxes/lifecycle.py`, inside #3's own package:

```
app/sandboxes/lifecycle.py:37   lifecycle_lease(...)
app/sandboxes/lifecycle.py:95   project_mirror_lock(...)
```

`store.py` holds only the primitives they wrap (`sandbox_lifecycle_lease` :3742,
`acquire_project_mirror_lock` :3831). A combined ordering-enforcing context manager
belongs in `lifecycle.py` and needs **no `store.py` change**. No #6 conflict. This
makes Q2's "enforce" answer considerably cheaper than the original implied.

### CORRECTION: "one context manager that takes both locks" would break staleness

The original's recommended enforcement — "one context manager that takes both locks in
the correct order and is the only way to get either" — **would break a deliberate case.**

All six `project_mirror_lock` call sites are in `router.py`. Five nest inside a lease;
one does not:

| Site | Operation | Inside a lease? |
|---|---|---|
| `:327`, `:340` | create | yes (lease at `:300`) |
| `:539` | **staleness** | **no — by design** |
| `:663` | sync | yes (lease at `:626`) |
| `:849` | publish | yes (lease at `:828`) |
| `:1157` | resume | yes (lease at `:1149`) |

`tests/sandboxes/test_staleness.py:140` asserts this explicitly:

```
test_staleness_does_not_take_a_lifecycle_lease_or_block_an_open_writer
```

So the invariant is not "always take both". It is **"if you take both, lease first"** —
mirror-lock-alone stays legal. Any combinator must permit the lock-only path. Design for
that, or the staleness test fails immediately and the design gets rewritten mid-flight.

There are eight `lifecycle_lease` sites in `router.py`: `:300`, `:626`, `:828`, `:1044`,
`:1101`, `:1149`, `:1304`, plus the import at `:23`.

---

## 5. The contrast to copy — with one caveat

`app/previews/router.py` is 529 lines and thin. Handlers are
`return _docker_response(lambda: <one service call>)`; `create_preview` at `:119` is 17
lines. The convention exists; `sandboxes/` just does not follow it.

**Caveat the original omitted:** `app/previews/service.py` is **3749 lines**. Copy the
*router's* thinness. Do not treat the service as a model of restraint — an unbounded
`sandboxes/service.py` is a real risk, and it is the reason to scope #3 tightly.

---

## 6. Open questions — updated recommendations

Resolve with the user before writing code. Changes from the original are marked.

1. **Scope.** All five handlers, or `publish_sandbox` + `sync_sandbox` first?
   *Recommend: publish + sync first.* They carry the invariant, and value is provable
   before committing to the rest. **Strengthened:** with `previews/service.py` at 3749
   lines as the cautionary case, a tight first slice matters more than the original
   implied.

2. **Enforce the lock order, or just relocate it?**
   *Recommend: enforce.* **Now cheaper than originally stated** — it lands entirely in
   `app/sandboxes/lifecycle.py`, needs no `store.py` change, and has no #6 conflict.
   **But** the combinator must allow the mirror-lock-only path that staleness uses
   (§4). Design it as "lease-then-lock, or lock alone", not "both or nothing".

3. **Error translation.** `sandboxes/router.py` has no `_docker_response` equivalent
   while seven other routers have one (that is candidate #8).
   *Recommend: unchanged — keep `HTTPException` in the router for this pass.* Do not
   pull #8 in. Three unmerged candidates is already the ceiling.

4. **The four `_require_v1_*` guards** (`:1429`, `:1439`, `:1451`, `:1463`).
   *Recommend: fold — unchanged.* **Verified:** all four are byte-identical except the
   409 message, and two of them (`_require_v1_staleness`, `_require_v1_sync`) differ
   only in the final word — "usable base commit; recreate it explicitly to use v1
   **staleness**" vs "**sync**". One rule, spelled four times.

5. **What the service returns.** Domain objects, or the Pydantic response models at
   `router.py:83-269` (186 lines)?
   *Recommend: domain objects; let the router encode — unchanged.* Otherwise the seam
   sits in the wrong place and the service still cannot be tested without HTTP shapes.

6. **Test strategy.** *Recommend: move workflow assertions down to the service, keep
   thin router tests for status codes and payload shape — unchanged.*
   **CORRECTION to the scope figure.** The original named only
   `tests/sandboxes/test_router.py` (1386 lines). The HTTP-driven surface is wider:

   | File | Lines | HTTP calls | Notes |
   |---|---|---|---|
   | `test_router.py` | 1386 | 49 | the bulk |
   | `test_sync.py` | 668 | 17 | sync workflow, HTTP-driven |
   | `test_staleness.py` | 378 | 6 | incl. the lock-order test at `:140` |
   | `test_publish.py` | 306 | 0 | already direct — tests `publish.py` |
   | `test_admission.py` | 682 | 0 | already direct — tests `lifecycle.py` |

   **72 HTTP-driven calls across three files**, not one. `test_publish.py` and
   `test_admission.py` already test leaves directly and need no change.

---

## 7. Watch out for

- **`FakeDockerClient`** — `tests/conftest.py`, now **436 lines** (original said ~388).
  Class at `:356`, fixture at `:390`, FastAPI dependency override at `:397`
  (original said `:396`). The override fires on `get_docker_client` from
  `app.docker_client`. **A service called directly bypasses it.** Decide how the client
  reaches the service — constructor argument or explicit parameter — *before* moving
  tests, or the failures will be confusing.
- **`_complete_database_provision`** (`router.py:1602`, 137 lines) is already a service
  function in the wrong file, called from three handlers. Still the cheapest first move
  and the best proof of the pattern.
- **Do not extend into the engine-detection handlers** while #2 is live (§1).
- `previews/service.py` at 3749 lines — a warning, not a target (§5).

---

## 8. Verification

```bash
cd /Users/jeromeagapay/orchestrator_v2/backend
.venv/bin/python -m pytest -q
```

**CORRECTION to the baseline.** The original recorded 758 passed / 43 skipped at
`fc274f4`. Current baseline on `refactor/hardened-boundary` + uncommitted work:

```
781 passed, 43 skipped, 1 warning in 30.32s
```

#1 and #2 added 23 tests. The tree is green *including* #2's uncommitted work — so #2
is functionally complete and unlanded, not mid-break.

There is **no linter and no type checker**. `pyproject.toml` declares pytest only.
Pytest is the whole gate.

The one warning is pre-existing and unrelated: Starlette deprecating `httpx` in
`TestClient`.

---

## 9. Artefacts

| What | Where |
|---|---|
| Architecture review, all 10 candidates (#3 is card 3) | `/var/folders/81/clx97fxj31vbs62x083t6t5c0000gn/T/architecture-review-20260818-142340.html` |
| Domain glossary | `CONTEXT.md` |
| Relevant ADRs | `docs/adr/0001`, `0003`, `0005`, `0007` |
| Original handoff (superseded) | `/var/folders/81/clx97fxj31vbs62x083t6t5c0000gn/T/handoff-sandboxes-service-module.md` |

Both `/var/folders` paths are macOS temp and can be deleted without warning. Copy
anything still needed into `docs/plans/`.

Treat any unverified number in the HTML report as suspect. Its card #2 originally
claimed a six-file `_NOT_YET_MIGRATED` ledger, 61 call sites, and three `_ensure_image`
copies; the true numbers were two, ~26, and two. The report file was corrected, but the
same inflation may survive elsewhere in it.

---

## 10. Suggested skills

1. **`improve-codebase-architecture`** — for shared vocabulary (module, interface,
   depth, seam, adapter, leverage, locality; the deletion test). Do not re-run the
   exploration; the report exists.
2. **`grilling`** — walk §6 with the user before writing code. Do not implement until
   they confirm.
3. **`domain-modeling`** — if the extracted module is named after a concept absent from
   `CONTEXT.md`, add the term there.

The `codebase-design` skill referenced by `improve-codebase-architecture` is **not
installed**. Use the vocabulary from the command body and
`~/.claude/skills/improve-codebase-architecture/HTML-REPORT.md`.

---

## 11. Working preferences

- Implementation goes to Codex; Claude reviews:
  `codex exec -m gpt-5.6-terra -c model_reasoning_effort="high" -s workspace-write "$(cat brief.md)" < /dev/null`
  — **`< /dev/null` is required**; `codex exec` hangs forever without it.
- Never pipe `codex exec` through `tail`. Hand the user
  `! python3 ~/.claude/bin/codex-tail.py` to watch the stream.
- Codex has no display and a blocked localhost. Never delegate rendered-UI checks to it.
- Commit messages: conventional subject, prose body explaining *why*, bullets for
  concrete changes, closing verification paragraph. See `8b55b24`.
  **Never add a Co-Authored-By or any Claude attribution line.**
- Branch before committing. `main` is the default branch.
- Prose is ASD-STE100-inspired: one idea per sentence, active voice, no vague cost
  phrasing — give real figures or write "unmeasured".

---

## 12. First three actions for the next session

1. Check whether `refactor/hardened-boundary` is committed and merged
   (`git status --short`, `git log --oneline main -1`). If not, stop and ask.
2. Ask the user which base #3 takes — `main`, or `refactor/hardened-boundary` (§1).
3. Run `grilling` over §6. Lead with Q2, because the staleness exception (§4) is the
   one decision that changes the shape of the code.
