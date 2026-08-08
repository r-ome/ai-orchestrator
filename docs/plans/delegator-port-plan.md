# Delegator: port plan onto the merged base

Branch `feat/delegator`, cut from `origin/main` at `879838e` (PR #1, which merged
`feat/task-branch-previews`). That base carries the Clarifier → Planner → Plan Reviewer flow,
the Task layer, and the planning UI. 251 backend tests pass on it.

The delegation layer was built earlier against pre-merge `main`, which lacked all of that. That
work is preserved on `wip/delegator-on-old-main` at `752f27a`. This plan says what moves across,
what is thrown away, and what still has to be written.

---

## Why there are two implementations

The original brief's Section 0 was accurate. It described `one_open_task_per_sandbox`,
`TASK_TRANSITIONS`, `run_turn_with_repair()`, `claim_planning_turn`, `PLANNING_CLAUDE_MODEL`,
`report_task_complete()`, and `schema_migrations` through 8 — every one of which exists on this
base, and none of which existed on pre-merge `main`.

Section 0 was verified against `main` instead of against all refs. `git log --oneline -3` and
`git status` were run; `git branch -a`, `git log --all`, and `git reflog` were not. The stale
`__pycache__` under `app/planning/` was read as "code that ran but was never committed" rather
than as a reason to check other refs. Fifteen true claims were reported as false, the brief was
rewritten around them, and roughly 13,700 lines were built, of which about a third duplicates
what was already here.

Revision 2 of the brief is therefore wrong in its Section 0 and should not be used. Revision 1
is the specification. This document is the delta.

---

## What the base already has

| | |
|---|---|
| Planning | `app/planning/` — 1,861 lines. Clarifier, planner, reviewer, a bounded review loop with `max_review_turns`, a findings ledger with a status lifecycle, and retained plan revisions |
| Tasks | `app/tasks/` — 989 lines. Branch per task, git-verified completion, accept with fast-forward, reject, preview integration |
| Turn runner | `app/planning/runner.py` — read-only container turns, `run_turn_with_repair()`, `TurnResult`, payload extraction |
| Store | `app/controller/store.py` — 1,645 lines, `planning_sessions`, `planning_messages`, `planning_findings`, `planning_plan_revisions`, `tasks`, migrations through 8 |
| UI | `PlanningSessionPage` (723), `ProjectPlanningSection` (323), `Markdown` (323), `PlanDiff` + `planDiff` (194), `PlanningTurnCard` (160), `PlanSpecView` (184), `PlanningRawOutput` (102), `Tabs`, `CollapsibleCard`, light/dark theme |

**None of that is rebuilt.** It is the base.

---

## What moves across

Roughly 6,900 lines, adapted to this base's schema, routes, and conventions.

### Ports almost unchanged — pure logic, no dependency on the old schema

| From `wip/delegator-on-old-main` | Lines | Change needed |
|---|---|---|
| `delegation/graph.py` | 312 | none — validator, waves, readiness, cycle detection are pure functions |
| `delegation/routing.py` | 120 | none — complexity ladder, precedence, weak-model warning |
| `delegation/packet.py` | 205 | field names only, to match this base's `PlanSpec` |
| `delegation/results.py` | 66 | none |
| `implementation_context/inventory.py` | 268 | none — reads `package.json`, `Makefile`, `pyproject.toml` and confirms commands against them |
| `implementation_context/validators.py` | 99 | none |

### Ports with adaptation

| From | Lines | Change needed |
|---|---|---|
| `delegation/{models,service,execution,prompts,config}.py` | ~1,555 | Session lookups move to this base's `planning_sessions` shape; `PlanSpec` gains `title`, `reviewer_outcome`, structured `risks`; routes nest under `/projects/{name}/…` to match planning |
| `implementation_context/{models,service,prompts,config}.py` | ~645 | Same session-shape adaptation |
| Delegation store methods + tables | ~450 | `delegations`, `work_items`, `work_item_runs`, `work_item_routing`, `implementation_contexts` — new tables, no collision |
| Migration runner | ~60 | Replaces the hardcoded inserts. New migrations start at **9**; this base's high-water mark is 8. The legacy-table retirement written earlier is not needed here and is dropped |
| Tests for all of the above | ~3,850 | Fixtures rebuilt against this base's session and task shapes |

### Written fresh against this base

- **Headless writable turn.** The base's runner is read-only (`ro` bind, `--permission-mode plan`).
  Delegation needs `rw` with write-capable flags. Extends `app/planning/runner.py` rather than
  adding a parallel module.
- **Headless task execution.** The base's tasks are agent-driven: a human types at an interactive
  agent, then calls `/tasks/{id}/report`. A delegated run needs to execute the turn itself. This
  is a new path through the existing task service, not a second task lifecycle (revision 1 §1.1).
- **`TurnResult` metrics.** Cost, token usage, and the model the provider reports it actually
  used. Needed for §4's success measure.
- **Tool-call outcome inspection.** A turn can exit 0 with every tool call failing and still
  answer. Measured on Codex, written up in the earlier ADR 0003, and worth carrying into this
  base's runner regardless of the rest.

---

## What is thrown away

| | Lines | Why |
|---|---|---|
| `app/planning/` (mine) | 2,030 | This base's is better: bounded review loop, findings ledger with responses per finding id, retained revisions, understanding confirmation, per-role providers |
| `app/tasks/` (mine) | 1,921 | This base's is more complete: preview integration, refusal messages, dirty-path handling |
| Planning UI (mine) | 992 | This base's is 2,155 lines with a Markdown renderer, revision diffs, and a raw-output viewer |
| Brief revision 2 | — | Its Section 0 is wrong |

---

## Open decision, needed before Phase 4 of the port

The base's task machine is:

```
OPEN       → REPORTED, REJECTED
REPORTED   → PREVIEWING            ← the only exit
PREVIEWING → REVIEW, FAILED
REVIEW     → ACCEPTED, REJECTED, FAILED
```

`accept_task` requires `REVIEW`, and `REVIEW` is only reachable through `PREVIEWING`, which
`start_preview` sets when a task-kind preview reaches running state. So **acceptance currently
requires a built, started preview stack**.

This is the trap revision 1 §0.7 named, and it asked for the choice to be stated before
implementing. It matters more for delegation than for a single task: a mid-graph work item may
leave the application unbuildable, and many units — a backend refactor, a migration, a shared
helper — have nothing meaningful to preview. Preview build timeouts reach 900s with a 30-minute
expiry.

**Decided: a non-preview verification path.**

`REPORTED → REVIEW` is added for a task whose completion the controller verified by reading the
branch, and whose configured verification commands passed. A preview stays available as a
separate, optional confirmation for items where one is meaningful. Existing agent-driven tasks
keep `REPORTED → PREVIEWING → REVIEW` exactly as they are, so nothing about the current human
flow changes.

`REPORTED → REJECTED` is added at the same time. Without it a task sitting in `reported` has one
exit only, and if that exit is unavailable it holds the sandbox's single task slot forever.

Rejected alternatives: a separate delegated task kind would mean two lifecycles to reason about,
which revision 1 §1.1 warns against; preview per work item is up to 900s per item and impossible
for units that leave the application unbuildable mid-graph.

---

## Order

1. ~~Decide the §0.7 question above.~~ Decided: non-preview verification path.
2. Store: delegation tables, migration runner, migrations from 9.
3. Writable turn on the base's runner, plus metrics and tool-call inspection.
4. Headless task execution, resolving §0.7.
5. Implementation context, including command confirmation.
6. Delegation persistence, graph, and the Delegator.
7. Packets, execution wiring, results.
8. Routing.
9. Verification and bounded recovery.
10. Delegation UI, following the planning UI's conventions, and the integration review.

---

## Where this stopped

Committed on `feat/delegator`: `5f48dfc`, steps 1 to 4 of the order above.
282 backend tests pass, 30 skipped, up from the base's 251.

**Step 5 is next: implementation context.** Port from
`wip/delegator-on-old-main` at `752f27a`:

| Take | To | Change |
|---|---|---|
| `backend/app/implementation_context/inventory.py` | same path | none |
| `backend/app/implementation_context/validators.py` | same path | none |
| `backend/app/implementation_context/{models,service,prompts,config}.py` | same path | session lookups use this base's `planning_sessions` (`project_id`, `title`, `plan_spec_json`, statuses `plan_ready` / `review_limit_reached`); the turn calls `run_planning_turn` from `app/planning/runner.py`, not the old `app/turns` |
| `backend/tests/implementation_context/*` | same path | fixtures rebuilt on this base's session shape |

Retrieve any file with `git show wip/delegator-on-old-main:<path>`. Do not
check that branch out; it is built on the pre-merge tree.

The store already carries `implementation_contexts`, so step 5 needs service
and router work only, plus its store accessors.

After that: delegation persistence and graph, the Delegator, packets and
execution wiring, routing, verification and recovery, then the delegation UI.

### Still open

- **Circular import.** `tasks.service` imports the runner at call time,
  because `tasks.runner` reaches `agents.service`, which imports
  `previews.service`, which imports `tasks.service`. The deferred import
  works and is commented. Moving `LABEL_CONTROLLER_MANAGED` and `LABEL_KIND`
  to a neutral module would remove the ring properly, and touches three
  existing files.
- **Verification commands are not run yet.** `verify_task` takes
  `verification_passed` and defaults it to true. Step 9 is what makes that
  argument mean something; until then review is gated on the git check alone.
- **Codex has no headless coding turn.** `run_coding_turn` returns 501 for it.
  Its own sandbox cannot start under `cap_drop ALL` and `no-new-privileges`.
  The fix is likely `--dangerously-bypass-approvals-and-sandbox`, relying on
  the container as the boundary, but that was never verified.
- **ADR numbering.** The earlier ADRs 0002 and 0003 on the wip branch clash
  with this base's 0002 to 0004. Renumber to 0005 and 0006 when ported.
