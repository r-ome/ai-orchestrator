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
2. ~~Store: delegation tables, migration runner, migrations from 9.~~
3. ~~Writable turn on the base's runner, plus metrics and tool-call inspection.~~
4. ~~Headless task execution, resolving §0.7.~~
5. ~~Implementation context, including command confirmation.~~
6. ~~Delegation persistence, graph, and the Delegator.~~
7. ~~Packets, execution wiring, results.~~
8. ~~Routing.~~
9. ~~Verification and bounded recovery.~~
10. ~~Delegation UI, following the planning UI's conventions, and the integration review.~~

---

## Port complete

Steps 1 to 4 are committed on `feat/delegator` at `5f48dfc`. Steps 5 to 10
are implemented in the working tree. The backend has 403 passing tests and
34 skipped tests. The frontend production build and lint check pass.

Implementation context now:

- accepts `plan_ready` and `review_limit_reached` planning sessions;
- reads `title` and `plan_spec_json` from this base's session shape;
- runs through `app/planning/runner.py` with the session's credential profile;
- confirms proposed commands against project manifests before retaining them;
- keeps one context per session, not revisions, and permits one repair turn;
- refuses to regenerate once the session has a delegation, which is what
  keeps a running delegation's context from changing underneath it;
- exposes one project- and session-scoped route: POST to generate, GET to read,
  returning null before a context has ever been generated.

Delegation now:

- stores each validated decomposition and its work items atomically;
- rejects duplicate keys, missing dependencies, self-dependencies, cycles,
  invalid fields, and unconfirmed verification command kinds;
- derives waves, readiness, blockers, and potential parallel work from the
  dependency graph and retained run attempts;
- allows one repair turn before rejecting an invalid generated graph;
- requires a ready implementation context with confirmed verification
  commands before spending a Delegator turn;
- keeps active-delegation and active-run limits in SQLite unique indexes;
- exposes project- and session-scoped create, generate, list, detail, and
  lifecycle routes.

Execution now:

- builds a bounded packet from the plan and the exact context retained by the
  delegation;
- resolves only controller-confirmed verification commands and carries
  accepted upstream results into dependent packets;
- starts each item through the existing task branch and writable turn path;
- adds the commit and structured-result contract at the headless task layer;
- retains the reported model, token use, cost, duration, exit code, and result
  payload for every attempt;
- keeps a successful run active and its dependants blocked until a person
  accepts and merges its task;
- rejects failed task branches, preserves retry history, and halts the
  delegation if task cleanup fails;
- exposes project-, session-, and delegation-scoped packet, run, accept, and
  reject routes.

Routing and recovery now:

- route by item override, run preference, complexity, then system default;
- retain the chosen provider, reported model, routing source, and warning;
- run confirmed commands in hardened, network-disabled containers without a
  shell interpreting the command;
- retry one provider failure once and run one focused repair after a
  verification failure;
- halt after repeated provider or verification failure;
- reopen only controller-reported tasks for focused repair.

Integration and UI now:

- retain feature-level review revisions against the reviewed plan, collected
  results, controller verification, and final repository state;
- validate review output and permit one output-repair turn;
- expose context generation, decomposition, waves, blockers, potential
  parallel work, routing overrides, runs, accept, reject, retry, and final
  review from one delegation page;
- poll active delegations and follow the planning UI's existing cards, pills,
  dialogs, API helpers, and route conventions.

### Still open

- **Circular import.** `tasks.service` imports the runner at call time,
  because `tasks.runner` reaches `agents.service`, which imports
  `previews.service`, which imports `tasks.service`. The deferred import
  works and is commented. Moving `LABEL_CONTROLLER_MANAGED` and `LABEL_KIND`
  to a neutral module would remove the ring properly, and touches three
  existing files.
- **Codex has no headless coding turn.** `run_coding_turn` returns 501 for it.
  Its own sandbox cannot start under `cap_drop ALL` and `no-new-privileges`.
  The fix is likely `--dangerously-bypass-approvals-and-sandbox`, relying on
  the container as the boundary, but that was never verified.
