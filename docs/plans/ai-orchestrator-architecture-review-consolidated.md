# Architecture-First Repository Review — `r-ome/ai-orchestrator`

**Reviewer stance:** senior/staff software engineer  
**Method:** repository inspected directly; import relationships and repeated patterns were checked programmatically in the stronger second pass. The test suite was not run. Findings are labeled **confirmed**, **likely**, or subjective where appropriate.

## Executive conclusion

The repository does **not** need a rewrite. Its underlying product and safety model is strong; most of the maintainability cost comes from unfinished migrations, misplaced responsibilities, cyclic package dependencies, and a few very large modules.

The most useful mental model is deliberately plural:

- **SQLite is authoritative for controller intent, lifecycle, approval, concurrency admission, and audit history.**
- **Git is authoritative for code, branches, commits, dirty state, and reviewed diffs.**
- **Docker is authoritative for which runtime resources actually exist.**
- **AI/provider output is untrusted until the controller independently verifies the resulting Git/runtime state.**

The controller's job is to reconcile these authorities safely. That separation is one of the best architectural decisions in the codebase and should survive any refactor.

The main problem is not essential domain complexity. It is that the source tree no longer exposes the domain model cleanly. Persistence, previews, sandbox orchestration, provider execution, and shared runtime utilities have accumulated responsibilities until developers must reconstruct cross-module relationships mentally.

The strongest recommendation is therefore:

> **Finish the migrations already in flight, delete what they superseded, then enforce a one-way dependency direction.**

A moderate restructuring remains the right destination, but subtraction is the first-order fix.

---

## Table of contents

1. [How the system works today](#1-how-the-system-works-today)
2. [Architecture mental model](#2-architecture-mental-model)
3. [Representative end-to-end flows](#3-representative-end-to-end-flows)
4. [What is already well-designed](#4-what-is-already-well-designed)
5. [Where humans will struggle](#5-where-humans-will-struggle)
6. [Consistency problems](#6-consistency-problems)
7. [Top simplification opportunities](#7-top-simplification-opportunities)
8. [Architecture options](#8-architecture-options)
9. [Recommended target architecture](#9-recommended-target-architecture)
10. [Refactoring roadmap](#10-refactoring-roadmap)
11. [Top five changes first](#11-top-five-changes-first)
12. [Overall assessment](#12-overall-assessment)
13. [Evidence index](#13-evidence-index)

---

# 1. How the system works today

## Core product model

AI Orchestrator is a **local control plane for an AI-assisted feature workspace**.

A person registers a Git source. A feature sandbox gets a writable Git workspace inside Docker-managed storage. Planning produces an approved implementation plan. That plan becomes implementation work. Work executes serially against isolated temporary branches, is independently verified, and is accepted into the feature branch only when Git and verification agree. The accumulated feature diff is then reviewed against pinned commits, optionally previewed, and eventually published upstream. Interactive coding-agent containers can also attach to the sandbox.

The authoritative state is split intentionally:

| State | Authority |
|---|---|
| Workflow lifecycle, approvals, orchestration history | SQLite |
| Source code, branches, commits, reviewed diffs | Git |
| Running containers, networks, volumes | Docker |
| Published remote branches / PR-side Git state | Git remote |
| Provider authentication | Provider-specific credential volumes |
| Browser/UI state | Frontend only; not authoritative |

Docker labels such as `orchestrator.managed`, `orchestrator.kind`, and `orchestrator.run-id` are the join mechanism that lets durable controller records be reconciled with real runtime resources.

## Major subsystems

### Projects

`backend/app/projects/`

Owns Git remote identity, registration, and canonical source/mirror relationships. It is comparatively small, but the word “project” is overloaded elsewhere in the API and persistence model, which creates confusion discussed later.

### Sandboxes

`backend/app/sandboxes/`

A sandbox is the durable feature workspace. It combines:

- a Git workspace and feature branch;
- Docker-backed storage;
- lifecycle state;
- optional database resources;
- engine detection;
- synchronization with the canonical source;
- eventual publication.

The sandbox lifecycle itself is explicit, with states such as `creating`, `awaiting_engine_confirmation`, `ready`, `syncing`, `publishing`, `database_failed`, `degraded`, `draining`, and `destroying`.

A sandbox is therefore not “a container.” It is a durable feature workspace whose code survives individual agent/container executions.

### Planning

`backend/app/planning/`

Planning turns a human request into an approved implementation plan. The lifecycle is explicit and guarded, with states such as `clarifying`, `awaiting_confirmation`, `planning`, `under_review`, `plan_ready`, `review_limit_reached`, `failed`, and `cancelled`, plus a separate turn state.

The workflow design is strong, but `planning/service.py` also owns process-local scheduling/provider concerns that are more general than planning.

### Implementation context

`backend/app/implementation_context/`

Builds repository inventory, commands, constraints, and other information needed by implementation work. Conceptually useful, but as a top-level peer of planning, delegation, and tasks it contributes to a vocabulary problem: several adjacent nouns describe one human-facing implementation journey.

### Delegation

`backend/app/delegation/`

Turns the approved plan into an executable dependency graph of work items, drives runs, verifies results, integrates accepted work, performs whole-feature review, and handles change requests.

The implementation model is deliberately serial. The sandbox has one writable Git workspace, the database enforces one open task, and parallel mutation would collide. `driver.py` derives ready work and executes it one item at a time.

The hardest file here is `delegation/execution.py`, which behaves like an application-level workflow coordinator while depending on planning data, implementation context, work-item metadata, routing, verification, task execution, AI turns, and persistence.

### Tasks

`backend/app/tasks/`

A task is effectively a **temporary Git mutation transaction inside a sandbox**:

1. establish the current Git baseline;
2. create an isolated task branch;
3. capture dirty-state information;
4. run coding work;
5. inspect actual Git state;
6. verify the result;
7. fast-forward accept or reject the task branch.

The coding agent's report is not authoritative. Git is.

The important vocabulary is:

> **Work item = requested implementation unit.**  
> **Work-item run = an attempt to perform that unit.**  
> **Task = the temporary Git branch transaction used to mutate the sandbox.**

Once explained, that hierarchy makes sense. The current names do not make it obvious enough.

### Feature review and delivery

`delegation/delivery.py` builds a commit-pinned feature target, verifies that successful work corresponds to accepted task commits, and protects against reviewing one state and publishing another. Change requests become further implementation work and feed back into review.

This is an important safety boundary: **review determines what may be published; publishing performs the external Git effect.**

### Previews

`backend/app/previews/`

Previews support detection, human review of a proposed runtime configuration, native/Dockerfile/Compose execution, dependency caches, networking, secrets, database services, logs, protected-file baselines, expiration, and cleanup.

The safety model is deliberately conservative: repository code is inspected to create a proposal, but it is not automatically executed. A person approves the exact proposal first, and protected-file changes invalidate earlier approval.

The problem is implementation concentration: `previews/service.py` is roughly 3,700 lines and owns several responsibilities that are not preview-specific.

### Agents

`backend/app/agents/`

Interactive coding agents are managed containers attached to a sandbox. They receive the sandbox workspace, provider-specific authentication, temporary writable state, hardened execution, and a detached terminal session. SQLite tracks the run; Docker determines whether the real container exists.

A database constraint prevents multiple active agent runs in one sandbox. Detached execution lets an agent survive browser disconnection, while startup reconciliation compares durable rows with actual Docker resources.

### Controller persistence and reconciliation

`backend/app/controller/`

The controller layer owns SQLite persistence and startup repair. `controller/store.py` contains schema, migrations, and persistence operations for nearly every subsystem. It also enforces important partial unique indexes. *(Phase 7 split this into the `controller/store/` package; see §5.2.)*

Startup reconciliation closes interrupted turns, reclaims stale leases, reconciles agents and previews, detects unexpected/orphaned resources, and expires previews.

This is the boundary where durable intent and ephemeral runtime reality are repaired after restart.

### Frontend

The React/Vite frontend roughly mirrors the product vocabulary: projects, sandboxes, planning, delegation/implementation, previews, agents, containers, and volumes.

Most API organization is sensible, but `DelegationWorkspace.tsx` and `PlanningSessionPage.tsx` have become workflow hotspots, and some backend lifecycle/query gaps are reconstructed in the browser.

## Four mechanisms to understand before changing anything

### 1. Hardened container execution

Every container routes through `run_hardened` or `create_hardened`. Security exceptions are expressed through named enum-like values such as provider egress, writable root filesystem, or database-server capabilities, making exceptions grep-able and reviewable.

### 2. Claim → background job → settle

Long operations atomically claim durable state, return `202`, then finish in an in-process background job. Progress is written as events. On restart, interrupted in-process work is settled rather than falsely resumed.

### 3. Partial unique indexes as admission control

Concurrency invariants such as one active agent, one open task, one running delegation run, or one active delegation per sandbox are enforced by SQLite rather than racy read-then-write checks.

### 4. Guarded state transitions

Several core workflows use explicit transition tables and guarded persistent updates. The store derives legal source states and includes them in the `UPDATE` predicate so validation and mutation are atomic.

These four mechanisms are architectural strengths, not cleanup targets.

---

# 2. Architecture mental model

```text
Browser (React/Vite)
   │ HTTP + WebSocket
   ▼
FastAPI routers ──► services/orchestrators ──► runners/executors
                         │                         │
                         │                         ▼
                         │                hardened container boundary
                         │                         │
                         ▼                         ▼
                   SQLite controller           Docker
                         │                         │
                         │                         └─ containers / networks / volumes
                         │
                         ├─ lifecycle / approval / audit / leases
                         │
                         └──────── reconcile ────────────────┐
                                                           │
Git workspaces / mirrors ◄─────────────────────────────────┘
   │
   ├─ branches / commits / dirty state
   └─ reviewed code truth
```

The shortest useful explanation to another engineer is:

> AI Orchestrator is a durable controller around one mutable feature workspace. SQLite records what the controller intends and what humans approved; Git proves what code actually exists; Docker proves which runtime resources actually exist. Planning produces implementation work, implementation executes serially through temporary Git branches, verified commits accumulate on the feature branch, the whole feature is reviewed against pinned commits, and only then is it published. Previews and interactive agents are controlled runtime attachments to that sandbox.

The product model is simpler than the source tree makes it appear. The package graph is where the explanation breaks down.

---

# 3. Representative end-to-end flows

## A. Create and operate a feature sandbox

Approximate path:

```text
Project / Sandbox UI
  → sandboxes API
  → sandboxes/router.py
  → acquire lifecycle lease
  → acquire project mirror lock
  → ensure canonical mirror
  → import/prepare workspace
  → create feature branch
  → detect engine
  → provision or confirm database
  → READY / confirmation / failure state
```

Lock ordering is explicit: sandbox lease before the project mirror lock.

## B. Plan a feature

```text
PlanningSessionPage
  → planning/router.py
  → planning/service.py
  → planning/runner.py
  → AI provider/container
  → clarifier turns
  → human confirmation
  → frozen brief
  → planner revision
  → reviewer findings ledger
  → PLAN_READY
```

Structured model calls use validation plus one repair attempt; invalid payloads are treated as results to handle, not automatically as untyped exceptions.

## C. Delegate and execute implementation

```text
Approved plan
  → implementation context
  → delegation create_revision
  → validate dependency graph / derive waves
  → claim run
  → driver.drive_delegation
  → execution.execute_run
      → build bounded work packet
      → task branch
      → coding turn
      → controller-confirmed verification
      → optional repair
      → accept / reject
```

At the individual work-item level:

```text
WorkItem
  → WorkItemRun
      → temporary task branch
      → capture baseline / dirty state
      → coding provider
      → inspect actual Git result
      → verify
      → fast-forward accept OR reject
```

This separation matters:

- the agent proposes/produces work;
- Git determines what actually changed;
- verification determines whether the change satisfies requirements;
- acceptance changes the feature branch.

## D. Preview a feature

```text
UI
  → detect runtime
  → proposed PreviewConfiguration
  → protected-file baseline
  → persist review round
  → HUMAN APPROVAL
  → validate exact proposal/baseline
  → prepare dependencies/services
  → isolated network
  → start native | Dockerfile | Compose runtime
  → optional database resources
  → logs / gateway / progress
  → PreviewRun in SQLite
  → expiration / stop / reconciliation
```

The detect → review → approve → execute sequence is a strong design choice because previewing an arbitrary repository is otherwise code execution by inspection.

## E. Start and communicate with a coding agent

```text
Frontend agents API
  → agents/router.py
  → agents/service.py
  → resolve sandbox
  → provider credential volume
  → workspace/runtime volumes
  → hardened Docker container
  → AgentRun in SQLite
  → detached tmux/terminal session
  ↔ WebSocket frontend
```

Managed Docker resources can survive backend restart. In-process AI turns generally cannot; reconciliation settles interrupted turns rather than pretending to resume them.

## F. Review and publish

```text
completed work items
  → delivery.py builds exact reviewed base/head target
  → whole-feature integration review
  → change requests, if any
  → re-review
  → sandbox publication endpoint
  → sandboxes/publish.py
  → mirror / remote push / optional PR
  → lifecycle update
```

The publication boundary is feature-level review, not the end of an individual coding-agent run.

---

# 4. What is already well-designed

These are worth defending during refactoring.

### Hardened execution boundary

`containers/hardened.py` is one of the strongest modules in the repository. Container security defaults are centralized, and exceptions are named rather than hidden at call sites.

### Explicit lifecycle machines

Planning, sandbox, task, and delegation lifecycles use explicit transition tables rather than scattered string assignments.

### Database-enforced concurrency invariants

Partial unique indexes enforce single-writer and single-active-run constraints at the database boundary.

### Git is treated as code truth

The system checks real branch state instead of trusting agent completion claims. Acceptance is tied to actual commits and fast-forward semantics; whole-feature review is pinned to concrete commits.

### Preview execution is approval-gated

Detection does not silently become execution. Exact proposals and protected-file baselines create a meaningful human approval boundary.

### Restart reconciliation acknowledges distributed truth

The system explicitly repairs the relationship between SQLite records and Docker reality after restart.

### Append-only execution history

Work-item runs, delegation revisions, and plan revisions preserve historical attempts rather than mutating away prior failures/costs.

### Serial implementation is intentional

The system avoids concurrency machinery that would be unsafe around one writable workspace. Serial execution is a deliberate simplification, not a limitation to “fix” by default.

### Useful local conventions already exist

`jobs.py` documents why threads are used; `CONTEXT.md` provides shared vocabulary; tests broadly mirror backend subsystems; the frontend's `useApiResource` hook already demonstrates a reusable load/refresh/error/poll/abort convention.

---

# 5. Where humans will struggle

## 5.1 There is no dependency direction (**confirmed — headline finding**)

Excluding `main.py` as a composition root, the package import graph contains one strongly connected component of nine packages:

```text
agents ⇄ controller ⇄ delegation ⇄ implementation_context ⇄
planning ⇄ previews ⇄ projects ⇄ sandboxes ⇄ tasks
```

Only `containers`, `volumes`, `turns`, and top-level shared modules are cleanly outside that component.

Concrete symptoms:

- **13 function-local imports** exist to break import cycles;
- `sandboxes/lifecycle.py` imports a **private** function across a package boundary from `tasks.service`;
- `controller/`, nominally persistence/reconciliation, imports multiple domain packages that import it back.

This means there is no safe reading order. Local reasoning is much harder than it should be.

## 5.2 Two god modules (**confirmed**)

### `controller/store.py`

Roughly 4,500 lines, one large class, about 155 methods, and roughly 30 tables. It contains schema, migrations, and persistence for nearly every product area.

The direct SQL style is fine. The ownership locality is not.

> **Resolved by Phase 7, 19 Aug 2026 (`main` @ `268fc2b`).** `controller/store.py` is now the
> package `controller/store/`, split into ten area mixins plus `schema`, `migrations`,
> `errors`, `queries` and `_shared`. The facade is 89 lines. The measured figures were 4,454
> lines, **151** methods and 33 tables — the "about 155 methods" above is close but the
> per-area distribution this review proposed was wrong in four places. See the Phase 7 status
> block in `architecture-review-verification-and-plan.md`. The direct SQL style, the single
> database, connection, lock and migration stream, and every partial unique index are
> unchanged.

### `previews/service.py`

Roughly 3,700 lines and about 100 top-level functions. It owns preview lifecycle, three runtime modes, Compose parsing/validation, dependency caching, gateway/networking, shared databases, schema attachment, and project-secrets CRUD.

Several of those concerns are not preview-specific at all.

> **Resolved by Phase 8, 19 Aug 2026, `main` @ `e7e84cc`.** Both figures were exact: 3,704
> lines and 104 top-level functions. `previews/service.py` is now **1,097 lines** and owns
> the lifecycle and proposal only. The other concerns this finding names moved out:
> `previews/runtimes/{native,compose,dockerfile,environment}.py`, `previews/sharing.py`,
> `previews/network.py`, `previews/protected_files.py`, `previews/resources.py`,
> `previews/health.py`, `previews/progress.py`, `previews/_shared.py`, `previews/errors.py`,
> plus `app/dependency_cache.py` and `app/projects/secrets.py`.
>
> "Several of those concerns are not preview-specific at all" is confirmed and only partly
> acted on. Project-secrets CRUD moved to the projects domain. Dependency caching moved to
> `app/dependency_cache.py`, which still imports eight names from `app.previews.*` and is
> therefore not yet neutral; Phase 9 moves it into `platform/`.

## 5.3 The sandbox router is a service in disguise (**confirmed**)

`sandboxes/router.py` is roughly 1,250 lines with around 50 raw `HTTPException`s. Create/resume/delete flows orchestrate leases, mirror locks, Git, engine detection, database provisioning, and lifecycle transitions directly in the route layer.

At the same time, `sandboxes/service.py` already owns `sync` and `publish` in the desired service shape. A planned service migration landed partially and stopped, leaving two conventions in one package.

Four helpers are duplicated byte-for-byte between router and service, and at least one rule is duplicated with different exception types.

## 5.4 Control flow depends on exception-message text (**confirmed**)

Sandbox recovery contains logic equivalent to:

```python
except RuntimeError as error:
    if "workspace is missing" not in str(error):
        raise
```

A wording change can therefore alter recovery policy. This should be a typed exception such as `WorkspaceMissing`.

## 5.5 “Project” means several different things (**confirmed**)

The API and persistence layers use “project” for:

- an actual Git remote/project;
- a route parameter that is effectively a sandbox id in planning/preview paths;
- a derived persistence key with legacy naming behavior.

A new engineer cannot infer from `/projects/{project_name}/planning/...` that `project_name` may really identify a sandbox.

## 5.6 Explicit state machines sit beside implicit/stringly ones (**confirmed**)

Core lifecycles have proper transition tables, while preview status and sandbox `operation` / `operation_phase` use free-form strings.

The second pass strengthened this finding dramatically: **sandbox manifest `operation_phase` is write-only state**.

Observed:

```text
reads of operation_phase in app code:  0
appearances in response models:        0
appearances in frontend/src:           0
write sites:                           8
distinct values written:              18
test assertions keeping it alive:     11
```

That is an undeclared 18-value state machine threaded through long handlers while protecting nothing.

Important distinction: the `operation` column on the **lease** table is read and should not be confused with the write-only manifest fields.

**Recommendation:** delete manifest `operation` / `operation_phase`; use the existing event stream for progress detail.

## 5.7 The legacy sandbox path is unreachable in production but dominates tests (**confirmed — biggest second-pass finding**)

The production path into legacy `store.register_sandbox` is effectively dead because the caller first resolves and validates an existing v1 sandbox, then returns before the legacy registration branch.

Yet test fixture usage was measured at roughly:

```text
test files using legacy register_sandbox: 30
test files using register_v1_sandbox:      7
```

So a large portion of the suite exercises a sandbox shape production can no longer create. Those fixtures keep legacy compatibility branches alive across planning, delegation, previews, implementation context, delivery, and database logic.

This changes the cleanup order: **convert tests to the v1 fixture first, then delete the legacy branches.**

## 5.8 Shared database-server logic exists twice (**confirmed**)

There are separate implementations in `previews/service.py` and `sandboxes/database.py`.

They duplicate naming, labels, get-or-create behavior, provisioning, and health logic, but differ in important ways: one is MySQL-specific and locked; the other is engine-generic and lacks the same process-wide lock/image-mismatch checks.

The duplication appears to exist primarily because previews still support the legacy sandbox shape. Retiring that shape removes a substantial amount of duplicated database infrastructure.

A race in the unlocked path is **likely** but was not reproduced; treat it as a bug to verify, not a confirmed runtime failure.

## 5.9 Names and ownership do not match responsibilities (**confirmed**)

Examples:

- `planning/runner.py` is actually a generic model-turn runner used outside planning;
- Docker label constants are defined in both previews and agents, then imported by unrelated modules from previews;
- generic log streaming for turns is exposed through a preview-named helper;
- agents depend on private preview dependency-cache helpers;
- preview configuration is used as a de facto owner of some shared runtime/Git settings.

These are strong signs that reusable runtime concepts live in the wrong feature package.

## 5.10 The implementation hierarchy is semantically correct but hard to discover

A developer must simultaneously understand:

- implementation context;
- delegation;
- work item;
- work-item run;
- task;
- coding turn;
- verification;
- integration review;
- change request.

The concepts are legitimate. The hierarchy is just not visible enough.

The clearest vocabulary would be:

```text
WorkItem
  → WorkItemRun
      → SandboxChange   # current internal Task concept
          → temporary branch
          → verify
          → accept/reject
```

A physical package consolidation can happen later; the vocabulary should become explicit earlier.

## 5.11 Frontend hotspots and missing regression protection (**confirmed**)

`DelegationWorkspace.tsx` is roughly 1,780 lines with many local state values, its own polling timer, and logic that reconstructs “what am I watching?” by scanning several in-flight row shapes. `PlanningSessionPage.tsx` is also over 1,200 lines.

The second pass found **zero frontend test files** under `frontend/src`. The current safety net is lint/build only.

That does not make the frontend architecture wrong, but it raises the risk of splitting these components. Add focused tests around watch-state/lifecycle projection before decomposing them.

## 5.12 Duplication is already drifting (**confirmed**)

Repeated helpers include `_integer`, `_progress`, `_docker_response`, `_object`, `_json_value`, `_base_branch`, and others.

The important point is not the count. The `_object` helpers have already diverged: one copy catches `TypeError` in addition to `ValueError`, while sibling copies do not. A bug was fixed once rather than everywhere.

Likewise, five delegation modules define near-identical `_progress` helpers even though their event-kind vocabulary is already centralized elsewhere.

## 5.13 Docker collection lookup is stringly typed (**confirmed**)

Sandbox cleanup selects Docker client collections using dynamic attribute construction such as:

```python
getattr(docker_client, f"{kind}s")
```

That requires type ignores and pushes invalid-kind failure to runtime. A small explicit mapping would be clearer and safer.

---

# 6. Consistency problems

| Area | Good convention already present | Where it breaks | Consolidated fix |
|---|---|---|---|
| Route error mapping | `docker_response(..., policy)` | sandboxes/projects/turns use inconsistent inline handling | Standardize on one route mapping pattern |
| Domain errors | repeated operation-error shapes | 14 near-identical classes plus a competing sandbox family | One shared `OperationError`; semantic subclasses only when behavior differs |
| Router thickness | thin routers delegating to services | sandbox create/resume/delete remain in routes | Finish service migration |
| Background work | claim → execute → settle → progress | repeated separately across several delegation workflows; planning has another scheduler | Share the real repeated mechanics, keep workflow-specific state |
| Model invocation | validated structured-turn path | tasks has separate provider CLI/output knowledge | One provider/turn runner with distinct result contracts |
| State transitions | enum + transition map + guarded update | preview status and manifest phases are strings | Use explicit state where state is real; delete write-only state |
| Docker labels | one managed-resource vocabulary | duplicated and owned by feature packages | Move to neutral platform/container module |
| Persistence | direct SQL through one controller | all domains in one 4,500-line class | Split methods by subsystem, keep one DB/lock/migration stream |
| Frontend data | `api/*.ts` + `useApiResource` | large workflow pages hand-roll polling/state projection | Centralize workflow view-model logic and add tests |

What is **not** a problem: per-package configuration organization, the hardened boundary, the basic test-tree mirroring, Git verification semantics, and the core state-authority model.

---

# 7. Top simplification opportunities

## 7.1 Finish the migrations already in flight

The second pass changes the priority order.

The repository appears to contain residue from at least three successful-but-incomplete migrations:

- v1 sandboxes replaced the legacy sandbox shape;
- service-layer orchestration replaced fat routers, but only for some sandbox workflows;
- the hardened container boundary replaced ad-hoc execution, while some shared vocabulary remained feature-owned.

The first move should therefore be **subtraction**:

1. convert legacy sandbox test fixtures to v1;
2. delete dead legacy branches;
3. delete write-only manifest operation state;
4. remove literal/duplicated helper definitions that only exist because both paths remain.

This reduces the amount of code that later restructuring has to move.

## 7.2 Establish one dependency direction

The target rule should be:

```text
platform  →  store  →  domain  →  api
```

where arrows represent “may be depended on by the layer to the right”; imports should point downward toward lower layers.

Concretely:

- move Docker labels, generic errors, jobs, and log framing out of feature packages;
- move provider command construction/stream parsing out of `planning/`;
- move startup reconciliation composition out of the persistence package;
- replace cross-package private imports with named public interfaces;
- add an import-direction test in CI so cycles cannot reappear.

This is the highest-level architectural fix because it restores a safe reading order.

## 7.3 Finish the sandbox service migration

Move create/resume/delete/confirm-engine/reset-db workflows out of `sandboxes/router.py` and into `sandboxes/service.py`.

The router should become:

```text
request validation
→ one service command
→ error/response mapping
```

Delete duplicated helpers and replace message-substring control flow with typed exceptions.

This is probably the highest value-per-risk local change.

## 7.4 Split `ControllerStore` by subsystem without redesigning the database

> **Done, Phase 7, 19 Aug 2026.** Every "keep" below held. The split shape chosen was mixin
> classes inherited by `ControllerStore`, not sub-repositories, so no call site changed in any
> of the 78 importing files. An AST oracle proved every method body byte-identical.

Keep:

- one SQLite database;
- one connection/locking strategy;
- one migration stream;
- the current partial unique indexes;
- the direct-SQL style.

Split only ownership/locality:

```text
store/
  connection.py
  migrations.py
  projects.py
  sandboxes.py
  planning.py
  implementation.py
  previews.py
  agents.py
  events.py
```

A thin compatibility facade can preserve `get_controller_store()` while callers migrate.

Do **not** combine this with a schema redesign or ORM/repository campaign.

## 7.5 Decompose previews after legacy removal

First remove the legacy shared-database path and extract project secrets. Then split the remaining preview concern into:

```text
previews/
  service.py          # lifecycle/use-cases
  proposal.py         # detection + approval
  lifecycle.py        # real preview state
  protected_files.py
  runtimes/
    native.py
    dockerfile.py
    compose.py
  network.py
```

Generic Docker/dependency/database primitives should live below the preview domain, not inside it.

Only introduce a typed `PreviewStatus` for lifecycle that is genuinely read. Do not replace one form of unused state with a more sophisticated unused state machine.

## 7.6 Clarify implementation vocabulary, then decide on physical consolidation

The conceptual hierarchy should be explicit immediately:

```text
ApprovedPlan
  → ImplementationContext
  → WorkItem
  → WorkItemRun
  → SandboxChange (current Task)
  → Verification
  → FeatureReview
  → Publication
```

A later package move can group `implementation_context`, `delegation`, and `tasks` under a visible `implementation/` umbrella once cycles are removed. Do the semantic cleanup before the large file move.

## 7.7 Standardize genuinely shared mechanics

Good candidates because repetition is already proven:

- one `OperationError` family;
- one progress-event helper keyed by the already centralized event-kind vocabulary;
- one shared JSON/object parsing helper where behavior is identical;
- one provider command/event parser with separate structured-output and code-edit contracts;
- one lightweight background-job facility rather than parallel planning/delegation scheduling conventions.

Do not introduce a generic workflow engine, event bus, Celery, Temporal, or distributed queue unless resumable durable workflows become a real requirement.

## 7.8 Stop reconstructing backend domain rules in the frontend

Prefer:

```text
feature API
→ feature view-model hook/reducer
→ page composition
→ focused components
```

Backend responses should expose authoritative lifecycle/action projections where the UI currently has to infer them. Add tests for the workflow projection logic before splitting the largest pages.

---

# 8. Architecture options

## Option A — Improve in place

Keep the current package layout and do only local cleanup: finish sandbox service migration, unify errors/labels, remove dead legacy state, and trim duplicates.

**Migration difficulty:** low.  
**Benefit:** meaningful local readability gains.  
**Risk:** the nine-package cycle survives, so the same ownership tangles can reappear.

This is a good short-term cleanup program but not the best steady-state architecture.

## Option B — Moderate restructuring (**recommended**)

Use four explicit layers:

```text
platform → store → domain → api
```

- shared Docker/jobs/errors/provider mechanics live below the domain;
- persistence is split by subsystem behind one SQLite owner;
- routers are thin;
- domain workflows own sequencing and rules;
- import direction is enforced by a test;
- the current state-authority model and safety semantics remain unchanged.

**Migration difficulty:** medium; mostly mechanical after the subtraction work.  
**Benefit:** the directory tree becomes a usable architecture diagram and local reasoning becomes possible.  
**Risk:** large diffs around store/package moves, mitigated by sequencing them separately from behavior changes.

**Chosen option: B.**

## Option C — Major redesign

A generic durable workflow engine could model operations as resumable steps with retries/compensation, but that would introduce another major abstraction, generic workflow schemas, execution semantics, and persistence rules.

Nothing found in either review justifies that cost. The existing choice to fail interrupted in-process AI work safely while reconciling durable Docker resources is defensible.

A rewrite or workflow-engine redesign would risk losing hard-won safety properties such as lock ordering, Git-pinned review, protected-file approval invalidation, tracked-database safeguards, and restart reconciliation.

**Reject Option C unless durable resumable workflows become a real product requirement.**

---

# 9. Recommended target architecture

The guiding rule is:

> **Domain features own decisions. Platform modules perform generic effects. Store modules record durable state. API modules translate protocols.**

A practical target:

```text
backend/app/
  platform/
    docker/             # client, hardened execution, labels, low-level resource helpers
    ai/                 # provider command construction + stream parsing
    dependencies/       # lockfile fingerprints, dependency caches/volumes
    jobs.py
    log_stream.py
    errors.py
    env.py

  store/
    connection.py
    migrations.py
    projects.py
    sandboxes.py
    planning.py
    implementation.py
    previews.py
    agents.py
    events.py

  domain/
    projects/
    sandboxes/
    planning/
    implementation_context/   # may later fold under implementation/
    delegation/               # may later fold under implementation/
    tasks/                    # current SandboxChange concept; may later fold
    previews/
    agents/
    turns/                    # only domain-level turn concepts, if any remain

  api/
    projects.py
    sandboxes.py
    planning.py
    implementation.py
    previews.py
    agents.py
    turns.py
    startup.py
    main.py
```

## Layer contracts

### `platform/`

Contains mechanics with no knowledge of product workflows.

Belongs here:

- Docker execution/hardening;
- shared label vocabulary;
- provider process/stream mechanics;
- dependency-cache mechanics;
- background-thread execution;
- log framing;
- generic error primitives.

Does **not** belong here: anything that needs to know what a sandbox, planning session, work item, preview approval, or agent run means.

### `store/`

SQL and durable-state access only.

Belongs here:

- schema/migrations;
- row mapping;
- guarded transitions;
- partial unique-index error translation;
- append-only event/history persistence.

Does **not** belong here: Docker calls, Git calls, HTTP status mapping, workflow sequencing.

### `domain/`

Owns rules, sequencing, lifecycle, and product vocabulary.

Routers should not reach around the domain layer to orchestrate Git/Docker themselves. Sibling domain dependencies should go through explicit public interfaces rather than private helper imports.

The **logical** implementation umbrella should read as:

```text
planning output
  → context
  → work items
  → runs
  → sandbox changes
  → verification
  → feature review
  → publication
```

Once dependency direction is clean, that logical umbrella can become a physical `domain/implementation/` package without mixing the file move with the more important behavioral cleanup.

### `api/`

Owns request/response models, dependency wiring, error mapping, WebSocket plumbing, and startup composition.

No orchestration longer than a single service command should live here.

## Desired dependency shape

Bad:

```text
Agent → Preview → Task → Agent
```

Target:

```text
Agent ────┐
Preview ──┼──→ platform.dependencies / platform.docker
Task ─────┘
```

Bad:

```text
TaskService → PreviewSettings → Git image
```

Target:

```text
TaskService → platform.git/docker settings
Preview     → platform.git/docker settings
```

Horizontal domain dependencies should be exceptional and explicit.

---

# 10. Refactoring roadmap

Each step should leave the repository shippable. Separate mechanical moves from behavior changes.

## Step 0 — Protect the invariants that define the architecture

Before broad movement, ensure tests clearly protect:

- legal lifecycle transitions;
- one-active-writer/agent/preview constraints;
- Git-based task acceptance and fast-forward semantics;
- dirty-state rejection;
- feature-review commit pinning;
- preview protected-file approval;
- startup reconciliation.

Also add the currently missing focused regressions for:

- missing-workspace recovery via a typed condition;
- frontend watch-state/lifecycle projection before splitting large components.

This is not a test-suite expansion campaign; it protects the safety properties that must not move accidentally.

## Step 1 — Move shared label vocabulary to a neutral module

**Goal:** remove one of the clearest wrong-owner imports.  
**Risk:** near zero.

## Step 2 — Collapse repeated operation-error classes

Create one shared `OperationError` primitive and use the existing route response mapper consistently. Keep semantic subclasses only where handling genuinely differs.

**Risk:** low.

## Step 3 — Delete duplicated sandbox helpers

Keep the service-owned version of duplicated rules and route all errors through the common mapping convention.

**Risk:** low.

## Step 4 — Replace message-driven workspace recovery with a typed exception

Introduce `WorkspaceMissing` (or equivalent) at the mirror/workspace boundary and remove string matching from router control flow.

**Risk:** low; add the focused recovery test first.

## Step 5 — Convert test fixtures to v1 sandboxes

Move the roughly 30 legacy-fixture test files to a shared `register_v1_sandbox` fixture/helper.

Do this as a dedicated mechanical commit with no production behavior changes.

**Risk:** low but noisy.

## Step 6 — Delete the legacy sandbox path

Remove dead legacy registration/backfill branches, legacy baseline coverage code, lifecycle-version guards that no longer serve production, and the preview fallback shared-database implementation that exists for the legacy shape.

**Expected simplification:** one sandbox representation and one shared-database implementation.

**Risk:** medium; now protected by v1-shaped tests.

## Step 7 — Delete write-only manifest operation state and consolidate drifting helpers

Remove sandbox manifest `operation` / `operation_phase` fields that have zero readers. Use events for progress detail.

In the same cleanup phase, consolidate proven identical helpers such as progress emission and shared object/JSON parsing while preserving the widest correct error behavior.

**Risk:** low.

## Step 8 — Finish the sandbox service migration

Move create/resume/delete/confirm-engine/reset-db bodies into `sandboxes/service.py`. First move behavior without simplification; then clean up once tests pass.

**Risk:** medium because these paths contain dense lock/lifecycle sequencing.

## Step 9 — Extract unrelated preview responsibilities

Move project secrets to the projects domain. After legacy deletion, centralize shared database ownership outside preview-specific code.

**Risk:** low-to-medium depending on database path changes.

## Step 10 — Split `ControllerStore` by subsystem

Keep one database, connection/lock model, migration stream, and all existing constraints. Expose a compatibility facade while methods move into store modules.

**Risk:** mechanically large, conceptually low.

## Step 11 — Introduce/enforce layer directories

Move neutral mechanics into `platform/`, persistence into `store/`, workflow composition into `domain/`, and protocol composition into `api/`.

Add an import-direction test so `store` cannot import `domain` and domain code cannot reach into `api`.

**Prerequisites:** shared labels/errors moved, sandbox service migration complete, store split underway/completed.

## Step 12 — Standardize provider turns and background-job mechanics

Move provider command construction and event-stream parsing into one neutral runner. Keep distinct result contracts for structured planning/review output versus writable code-edit turns.

Consolidate the repeated claim/execute/settle/progress mechanics only where the pattern is truly identical.

**Do not** introduce a generic durable workflow engine.

## Step 13 — Clarify implementation hierarchy

Rename the internal task-branch concept toward `SandboxChange` while preserving wire compatibility if necessary.

Once dependencies are clean, optionally consolidate:

```text
implementation_context → domain/implementation/context
delegation/*            → domain/implementation/*
tasks                    → domain/implementation/sandbox_change
```

Make this a structural move, not a behavioral rewrite.

## Step 14 — Simplify frontend workflow composition

Add tests for the “what am I watching?” projection, then split `DelegationWorkspace.tsx` and `PlanningSessionPage.tsx` around one workflow/view-model layer rather than many small competing hooks.

Where the browser currently reconstructs server ownership rules, improve backend projections/query boundaries instead.

---

# 11. Top five changes first

1. **Move sandbox create/resume/delete into `sandboxes/service.py`; delete duplicated helpers.** This is the best value-per-risk readability win and finishes an already-started migration.
2. **Add an import-direction guard; move shared labels and generic model-turn mechanics to neutral homes.** Cheap architectural guardrails stop the cycle from getting worse while deeper work proceeds.
3. **Collapse repeated exception types into one error convention and use one route-mapping pattern.** One rule replaces many copies with little behavioral risk.
4. **Retire the legacy sandbox shape — convert test fixtures first, then delete the branches.** This is the largest confirmed deletion opportunity and removes the duplicate shared-database implementation as a side effect.
5. **Delete sandbox manifest `operation_phase` / related write-only state.** Eighteen undeclared phase values, zero readers, and they are threaded through the exact workflows being moved in #1.

The `ControllerStore` split remains an important medium-term move, but the second pass pushes it below the top five because it is a large mechanical refactor whose benefit accrues gradually, while #4 and #5 are immediate deletions.

---

# 12. Overall assessment

Nothing in the deeper review argues for a rewrite. If anything, the second pass strengthens the case against one.

The confusing parts of the repository are overwhelmingly **residue from migrations that were implemented in the right direction but never fully retired their predecessors**. A maintainer is often learning one architecture and a half:

- v1 sandboxes plus legacy sandbox branches;
- service-layer workflows plus fat route handlers;
- hardened shared runtime boundaries plus feature-owned shared utilities;
- explicit lifecycle machines plus write-only/stringly state.

That is why the codebase looks more conceptually complicated than the product model actually is.

The durable safety philosophy should remain:

1. A sandbox is one feature workspace.
2. SQLite records controller intent, approval, admission, and history.
3. Git proves what code actually exists.
4. Docker proves what runtime resources actually exist.
5. Only one implementation writer mutates a sandbox at a time.
6. Planning creates an approved plan; implementation turns it into verified commits.
7. A work item is logical work; a sandbox change is the temporary Git transaction used to perform it.
8. Previews and agents consume shared platform/runtime capabilities rather than depending on one another.
9. Lower-level runtime/store code does not import product workflows.
10. Feature review is attached to concrete Git state before publication.

The repository does not need a more sophisticated architecture. It needs its existing architecture to become visible.

The essential complexity — Git safety, approval gates, container isolation, provider execution, databases, and crash reconciliation — is mostly legitimate. The accidental complexity comes from **unfinished deletion, unclear ownership, and packages being allowed to know too much about one another**.

The best end state is not “fewer files” or “more abstractions.” It is that an engineer working on preview execution can remain inside previews plus shared platform modules; an engineer working on planning can remain inside planning plus model/runtime/store boundaries; and an engineer working on implementation can follow one obvious hierarchy from plan to verified Git state without mentally joining several unrelated package names.

---

# 13. Evidence index

| Claim | Evidence captured in the reviews |
|---|---|
| 9-package import cycle | computed SCC over `from app.<pkg>` edges, excluding `main.py` |
| 13 cycle-breaking local imports | local-import scan across `backend/app` |
| Cross-package private import | `sandboxes/lifecycle.py` importing `_stop_task_preview` from tasks |
| `store.py` size/concentration | ~4,511 lines, 155 methods, ~30 tables |
| `previews/service.py` size/concentration | ~3,709 lines, ~100 top-level functions |
| Sandbox router thickness | ~1,255 lines, ~50 inline `HTTPException`s |
| Duplicated sandbox helpers | four byte-identical helpers in router/service |
| Substring-driven control flow | missing-workspace recovery in `sandboxes/router.py` |
| Project/sandbox naming mismatch | `projects/service.py` resolves route “project” to a sandbox row |
| `operation_phase` write-only | 0 app reads, 8 writes, 18 values, 11 test assertions |
| Two shared-DB implementations | preview service vs sandbox database module |
| Legacy path unreachable in production | production call trace through `ensure_sandbox_registered` |
| Legacy fixture dominance | ~30 test files legacy vs ~7 v1 |
| Repeated progress helpers | five near-identical delegation `_progress` functions |
| Helper drift | `_object` copies differ in exception handling |
| Stringly Docker collection lookup | dynamic `getattr(..., f"{kind}s")` in sandbox cleanup |
| Frontend test gap | no `*.test.*` files under `frontend/src` in the inspected tree |
| Hardened execution coverage | all container execution call sites route through hardened helpers |

