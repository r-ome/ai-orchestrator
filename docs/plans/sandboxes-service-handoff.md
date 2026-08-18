# Handoff — Candidate #3: give `app/sandboxes` a service module

**Repo:** `/Users/jeromeagapay/orchestrator_v2`
**Base:** `main` @ `13c85f3` — candidates #1 and #2 are merged
**Verified:** 18 Aug 2026, against `main` @ `13c85f3`, clean tree

Every figure below was checked against `13c85f3` directly. Where an earlier draft of
this document was wrong, the entry is marked **WAS WRONG** so the error is not repeated.

---

## 1. Starting state — clear to begin

Candidates #1 and #2 are merged and pushed. `main` is `13c85f3`.

```
13c85f3  #2  refactor(containers): finish the hardened boundary migration
2118409  #2  refactor(containers): route the four small container sites through the boundary
2776b74  #2  refactor(containers): widen the hardened boundary to every container
65668b4  #1  refactor(turns): run every validated turn through one module
a9588a9  #1  refactor(delegation): name the verification repair prompt for what it is
fc274f4      previous main
```

#1 was an ancestor of #2, so one fast-forward landed both: 41 files, +1922 / −925.
`refactor/hardened-boundary` and `refactor/validated-turn` are now fully contained in
`main` and can be deleted.

**Branch #3 from `main`.** The base question that earlier drafts left open is settled —
there is no longer a second candidate to stack on.

### What #2 left in #3's path

**WAS WRONG.** An earlier draft said #2 never touched `sandboxes/database.py`. The final
commit `13c85f3` does touch it: +94 / −98.

It does not matter, and here is the check that proves it. The module's public API is
byte-identical between `fc274f4` and `13c85f3`:

```bash
diff <(git show fc274f4:backend/app/sandboxes/database.py | grep -E "^(def|class) " | grep -v "^def _") \
     <(git show 13c85f3:backend/app/sandboxes/database.py | grep -E "^(def|class) " | grep -v "^def _")
# no output
```

Only private helpers moved: `_ensure_image` removed, `_run_database_command` added. A
service calling `database.py` can rely on the signatures as they stand.

**`app/sandboxes/router.py` was not touched by #2 at all.** #3 starts on a clean file.

`tests/sandboxes/test_router.py` took a 12-line rename (+12 / −12), nothing structural.

---

## 2. The problem

`app/sandboxes/` is the only package with no `service.py`. Every neighbour has one
(`previews/`, `projects/`, `tasks/`, `planning/`, `delegation/`, `agents/`,
`implementation_context/`). The workflows live in the route handlers instead, so they
are reachable only through HTTP and testable only through a test client.

---

## 3. Evidence

### Handler sizes

`app/sandboxes/router.py` is 1753 lines. Five handlers plus one helper hold 948:

```
 271- 441   170 lines  create_or_resolve_sandbox
 512- 589    77 lines  get_sandbox_staleness
 590- 780   190 lines  sync_sandbox
 781- 999   218 lines  publish_sandbox
1135-1291   156 lines  resume_sandbox
1602-1739   137 lines  _complete_database_provision   <- service function in the wrong file
                       ------
                       948
```

### Leaf modules — capabilities, not orchestration, and fine as they are

```
1625  database.py      872  git.py           482  engine_detection.py
 401  publish.py       260  lifecycle.py     176  naming.py
 146  mirror.py         98  orphans.py        62  manifest.py
```

Three of these shifted under #2 (`database.py` 1629→1625, `git.py` 862→872,
`engine_detection.py` 477→482). Counts in any older draft are stale; these are current.

---

## 4. The lock-ordering invariant — the load-bearing part

### Four prose statements, zero enforcement

```
app/controller/store.py:694     "callers must acquire a sandbox lease first and this project lock second"
app/controller/store.py:3828    "Lock order for callers that need both locks is: sandbox lease first, then this project mirror lock"
app/sandboxes/router.py:826     "Lock order is fixed: sandbox lifecycle lease, then project mirror lock"
app/sandboxes/lifecycle.py:105  "Callers holding both locks must enter lifecycle_lease first and this context second"
```

The third lives inside an HTTP handler. Nothing detects a violation.

### Enforcement stays inside `app/sandboxes/` — no `store.py` change

**WAS WRONG.** An earlier draft warned that enforcing lock order "reaches into
`controller/store.py`, which candidate #6 also touches."

It does not. Both context managers already live in #3's own package:

```
app/sandboxes/lifecycle.py:37   lifecycle_lease(...)
app/sandboxes/lifecycle.py:95   project_mirror_lock(...)
```

`store.py` holds only the primitives they wrap (`sandbox_lifecycle_lease` :3742,
`acquire_project_mirror_lock` :3831). A combined, ordering-enforcing context manager
belongs in `lifecycle.py` and needs no `store.py` change. **No #6 conflict.** This makes
enforcement considerably cheaper than it first appeared.

### The invariant is conditional — a naive combinator breaks staleness

**WAS WRONG.** An earlier draft proposed "one context manager that takes both locks in
the correct order and is the only way to get either." That breaks a deliberate case.

All six `project_mirror_lock` call sites are in `router.py`. Five nest inside a lease.
One does not:

| Site | Operation | Inside a lease? |
|---|---|---|
| `:327`, `:340` | create | yes — lease at `:300` |
| `:539` | **staleness** | **no — by design** |
| `:663` | sync | yes — lease at `:626` |
| `:849` | publish | yes — lease at `:828` |
| `:1157` | resume | yes — lease at `:1149` |

`tests/sandboxes/test_staleness.py:140` asserts the exception by name:

```
test_staleness_does_not_take_a_lifecycle_lease_or_block_an_open_writer
```

So the rule is **"if you take both, lease first"** — not "always take both".
Mirror-lock-alone must stay legal. Design the combinator for that, or the staleness test
fails on first run and the design gets rewritten mid-flight.

Eight `lifecycle_lease` sites in `router.py`: `:300`, `:626`, `:828`, `:1044`, `:1101`,
`:1149`, `:1304`, plus the import at `:23`.

---

## 5. The contrast to copy — and its limit

`app/previews/router.py` is 529 lines and thin. Handlers are
`return _docker_response(lambda: <one service call>)`; `create_preview` at `:119` is 17
lines. The convention already exists in this codebase.

**Copy the router's thinness, not the service.** `app/previews/service.py` is **3772
lines**. It is the cautionary case, not the target, and it is the reason to scope #3
tightly.

---

## 6. Open questions for the grilling

Resolve with the user before writing code. Recommendations are given; the decision is
theirs.

1. **Scope.** All five handlers, or `publish_sandbox` + `sync_sandbox` first?
   *Recommend: publish + sync first.* They carry the invariant, so the value is provable
   before committing to the rest. With `previews/service.py` at 3772 lines as the
   warning, a tight first slice matters.

2. **Enforce the lock order, or relocate the comment?**
   *Recommend: enforce.* Relocating prose is not deepening. It lands entirely in
   `app/sandboxes/lifecycle.py` and needs no `store.py` change (§4) — but the combinator
   must permit the mirror-lock-only path staleness uses. **Grill this one first: it is
   the decision that changes the shape of the code.**

3. **Error translation.** `sandboxes/router.py` has no `_docker_response` equivalent
   while seven other routers have one (that is candidate #8).
   *Recommend: keep `HTTPException` in the router for this pass.* Do not pull #8 in.

4. **The four `_require_v1_*` guards** (`:1429`, `:1439`, `:1451`, `:1463`).
   *Recommend: fold into one.* Verified byte-identical except the 409 message, and two
   of them differ only in the final word — "…recreate it explicitly to use v1
   **staleness**" vs "**sync**". One rule, spelled four times.

5. **What the service returns.** Domain objects, or the Pydantic response models at
   `router.py:83-269` (186 lines)?
   *Recommend: domain objects; let the router encode.* Otherwise the seam sits in the
   wrong place and the service still cannot be tested without HTTP shapes.

6. **Test strategy.** *Recommend: move workflow assertions down to the service, keep
   thin router tests for status codes and payload shape.*

   The HTTP-driven surface is wider than one file — **72 calls across three**:

   | File | Lines | HTTP calls | Notes |
   |---|---|---|---|
   | `test_router.py` | 1386 | 49 | the bulk |
   | `test_sync.py` | 668 | 17 | sync workflow |
   | `test_staleness.py` | 378 | 6 | incl. the lock-order test at `:140` |
   | `test_publish.py` | 306 | 0 | already direct — tests `publish.py` |
   | `test_admission.py` | 682 | 0 | already direct — tests `lifecycle.py` |

   `test_publish.py` and `test_admission.py` already test leaves directly. Leave them.

---

## 7. Watch out for

- **`FakeDockerClient`** — `tests/conftest.py`, 436 lines. Class at `:356`, fixture at
  `:390`, FastAPI dependency override assigned at `:408`. It overrides
  `get_docker_client` from `app.docker_client`. **A service called directly bypasses
  it.** Decide how the client reaches the service — constructor argument or explicit
  parameter — *before* moving tests, or the failures will be confusing.
- **`_complete_database_provision`** (`router.py:1602`, 137 lines) is already a service
  function in the wrong file, called from three handlers. Cheapest first move and the
  best proof of the pattern.
- `previews/service.py` at 3772 lines — a warning, not a target (§5).

---

## 8. Verification

```bash
cd /Users/jeromeagapay/orchestrator_v2/backend
.venv/bin/python -m pytest -q
```

Baseline at `13c85f3`:

```
785 passed, 43 skipped, 1 warning in 28.29s
```

There is **no linter and no type checker**. `pyproject.toml` declares pytest only.
Pytest is the whole gate.

The single warning is pre-existing and unrelated: Starlette deprecating `httpx` in
`TestClient`.

---

## 9. Artefacts

| What | Where |
|---|---|
| Architecture review, all 10 candidates (#3 is card 3) | `/var/folders/81/clx97fxj31vbs62x083t6t5c0000gn/T/architecture-review-20260818-142340.html` |
| Domain glossary | `CONTEXT.md` |
| Relevant ADRs | `docs/adr/0001`, `0003`, `0005`, `0007` |

The `/var/folders` path is macOS temp and can be deleted without warning. Copy the
report into `docs/plans/` if it is still needed.

**Treat any unverified number in that report as suspect.** Its card #2 claimed a
six-file `_NOT_YET_MIGRATED` ledger, 61 call sites, and three `_ensure_image` copies;
the true numbers were two, ~26, and two. The file was corrected, but the same inflation
may survive elsewhere in it. The exploration agents that produced it overstated figures
more than once.

---

## 10. Suggested skills

1. **`improve-codebase-architecture`** — for shared vocabulary (module, interface,
   depth, seam, adapter, leverage, locality; the deletion test). Do not re-run the
   exploration; the report exists.
2. **`grilling`** — walk §6 with the user before writing code. Do not implement until
   they confirm shared understanding.
3. **`domain-modeling`** — if the extracted module is named after a concept absent from
   `CONTEXT.md`, add the term there as you go.

The `codebase-design` skill referenced by `improve-codebase-architecture` is **not
installed** on this machine. Use the vocabulary from the command body and
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

## 12. First three actions

1. `git checkout main && git pull` — confirm `13c85f3` or later, clean tree.
2. `git checkout -b refactor/sandboxes-service`.
3. Run `grilling` over §6, leading with Q2. The staleness exception (§4) is the one
   decision that changes the shape of the code.
