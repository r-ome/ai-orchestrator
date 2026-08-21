# AGENTS.md

This file defines the shared working rules for coding agents in this repository.

These instructions apply to Claude Code, Codex, and other coding agents unless a more specific instruction file overrides them.

## 1. Start here

Before making a non-trivial change:

1. Read `CONTEXT.md`.
2. Read the relevant source code before proposing changes.
3. Read the relevant README:
   - `README.md` for the system overview.
   - `backend/README.md` for backend, runtime, and preview behavior.
   - `backend/agent-images/README.md` for coding-agent images.

4. Check `docs/adr/` when changing architecture, security boundaries, persistence, execution, or lifecycle behavior.
5. Check `docs/plans/` when the task relates to ongoing design work.

Do not infer architecture from filenames alone. Trace the relevant request or lifecycle through the code.

## 2. System mental model

Orchestrator is a local control plane for AI-assisted software development.

```text
React + Vite
     |
     | HTTP / WebSocket through /api
     v
FastAPI controller
     |
     +-- controller-owned SQLite
     +-- Docker sandboxes
     +-- coding agents
     +-- planning and delegation
     +-- preview stacks and databases
     +-- Git repositories
     `-- Claude and Codex providers
```

The key ownership rule is:

- Git and sandbox files are authoritative for code state.
- Docker is authoritative for runtime state.
- Controller-owned SQLite is authoritative for workflow, approval, lifecycle, and audit state.

Do not blur these responsibilities.

## 3. Repository layout

```text
backend/
  app/
    agents/                  coding-agent lifecycle
    containers/              Docker container inspection/control
    controller/              controller-owned state and shared control logic
    delegation/              approved-plan execution
    implementation_context/  repository context used by delegation
    planning/                clarification, planning, and plan review
    platform/                lower-level platform helpers
    previews/                preview proposal and runtime lifecycle
    projects/                registered repositories
    sandboxes/               isolated project sandboxes
    tasks/                   sandbox coding tasks
    turns/                   model/container turns
    volumes/                 Docker volume inspection/control
    main.py                  FastAPI application
    startup.py               startup reconciliation

  agent-images/              Claude and Codex runtime images
  tests/                     backend tests

frontend/
  src/
    api/                     backend API integration
    components/              reusable UI
    hooks/                   React hooks
    pages/                   route-level UI
    test/                    test support
    utils/                   shared helpers
    App.tsx                  application routing/shell

docs/
  adr/                       architecture decisions
  plans/                     design and implementation notes

CONTEXT.md                    canonical domain language
```

Keep functionality in the domain module that owns it. Avoid turning `main.py`, shared utility modules, or React page components into catch-all layers.

## 4. Use the repository language

`CONTEXT.md` is the source of truth for domain terminology.

Use its terms exactly when they refer to defined concepts.

Important distinctions include:

- **Sandbox** — editable snapshot of a registered project.
- **Coding agent** — the single writable automation session assigned to a sandbox.
- **Reviewer** — read-only sandbox participant.
- **Planning session** — one attempt to create a reviewed implementation plan.
- **Clarifier** — gathers understanding; it does not write the plan.
- **Planner** — creates plan revisions.
- **Plan reviewer** — reviews a plan revision.
- **Feature brief** — frozen controller-assembled understanding of the feature.
- **Implementation context** — controller-built repository description used during delegation.
- **Delegation** — implementation of one approved plan.
- **Work item** — one unit of an approved plan.
- **Work item run** — one attempt at a work item.
- **Task** — one coding-agent turn on a temporary branch.
- **Verification** — controller-run evidence for declared commands.
- **Hardened run** — isolated execution under the hardened runtime boundary.
- **Turn** — a hardened run that invokes a model.
- **Feature review** — review of an exact implemented commit range.
- **Feature change request** — post-delegation request against the complete implementation.
- **Preview proposal** — non-executable description of a proposed preview.
- **Preview stack** — the active application runtime for a sandbox.
- **Source merge** — explicit fast-forward delivery into the original project folder.

Do not invent synonyms for these concepts in APIs, models, UI copy, tests, or documentation.

In particular:

- A **Task belongs to a Sandbox**, not a Delegation.
- A Work item run or Feature change request may point to a Task.
- Do not use `task`, `work item`, and `job` interchangeably.

## 5. Preserve the control-plane invariants

Changes must preserve the following unless the task explicitly changes the architecture and the corresponding decision is documented.

### Human approval

Do not bypass explicit approval boundaries.

In particular:

- Preview detection must not execute project code automatically.
- A preview runs only after approval of the exact proposal.
- Destructive Docker operations must remain explicitly confirmed.
- Replacing an active coding agent must remain explicit.
- Source delivery requires the appropriate approved feature review.

Prefer a visible failure over silently weakening an approval requirement.

### Exact-state review

Reviews and approvals must refer to the state that was actually reviewed.

Do not weaken checks that bind review or delivery to:

- exact branches;
- exact base and head commits;
- protected runtime-file contents;
- dirty-worktree baselines;
- current sandbox state.

If relevant state changes, invalidate stale approval instead of trying to reuse it.

### Isolation

Treat sandbox, agent, preview, credential, and controller boundaries as security boundaries.

Do not casually introduce:

- Docker socket mounts into coding agents;
- host home-directory mounts;
- host bind mounts into isolated previews;
- privileged containers;
- host networking;
- arbitrary capabilities or devices;
- controller secrets in sandbox files;
- project `.env` files entering controlled preview runtimes;
- unapproved outbound runtime networking.

A convenience change that weakens isolation is not a refactor.

### Credentials

Provider credentials belong in provider-specific credential volumes.

They must not be written into:

- the repository;
- project/sandbox volumes;
- Docker images;
- API request payloads;
- logs;
- container command arguments.

Never commit real values from `backend/.env`.

### Controller-owned state

Coding agents and preview applications must not gain write access to trusted controller metadata.

Do not make workflow correctness depend on mutable files inside the sandbox when the state belongs to the controller.

## 6. Backend conventions

The backend uses Python 3.11+, FastAPI, pytest, and Ruff.

Prefer the existing domain-module structure:

```text
router / API boundary
        |
        v
domain service / lifecycle logic
        |
        +-- controller state
        +-- Docker / Git / provider boundary
        `-- domain models
```

When changing backend behavior:

- Keep HTTP concerns near routers.
- Keep lifecycle and business rules outside route handlers when practical.
- Keep persistence decisions explicit.
- Reuse existing controller stores and domain models before adding parallel abstractions.
- Keep Docker and Git side effects behind their existing boundaries.
- Make state transitions explicit and testable.
- Preserve startup reconciliation behavior for durable resources.
- Prefer typed models over unstructured dictionaries when a stable contract exists.
- Return actionable errors rather than hiding inconsistent state.

Do not add a second source of truth for existing controller state.

### Backend style

Ruff is configured for Python 3.11 with an 88-character line length.

Follow nearby code before introducing new conventions.

Avoid:

- unrelated formatting changes;
- speculative abstractions;
- large generic helper modules;
- hidden global state;
- broad exception swallowing.

## 7. Frontend conventions

The frontend uses React, TypeScript, Vite, React Router, Vitest, Testing Library, and Oxlint.

Keep responsibilities separated:

- `api/` handles backend communication and API contracts.
- `components/` holds reusable presentation or interaction units.
- `hooks/` holds reusable React behavior.
- `pages/` composes route-level workflows.
- `utils/` holds genuinely shared non-React helpers.

Do not duplicate API request logic across page components when it belongs in `api/`.

Preserve the backend's safety model in the UI.

The UI must not make an operation appear safe or complete before the backend has established that state.

Important examples include:

- preview approval;
- protected-file changes;
- verification evidence;
- feature review;
- awaiting-review changes;
- source merge;
- destructive Docker actions.

### Frontend tests

Tests use Vitest and jsdom.

Keep tests next to the code they cover using:

```text
*.test.ts
*.test.tsx
```

Prefer behavioral assertions over assertions about implementation details.

## 8. Agent image rules

`backend/agent-images/` defines the Claude and Codex coding environments.

These images contain tooling, not project data or credentials.

The default images already provide pinned Playwright and Chromium.

Do not install browser test infrastructure into a user's project merely because an agent needs browser verification.

Coding-agent containers are intentionally restricted.

Their writable locations are limited to:

```text
/workspace
/auth
/tmp
```

Preserve the read-only root filesystem and credential separation.

After changing an agent Dockerfile, rebuild the affected image before relying on the change.

## 9. Change discipline

Make the smallest coherent change that solves the requested problem.

Before editing:

1. Identify the owning module.
2. Trace the existing behavior.
3. Find the relevant tests.
4. Identify any security, approval, or persistence invariant involved.

During editing:

- Match existing architecture before creating a new abstraction.
- Preserve backward-compatible persisted state when required.
- Update tests with behavior changes.
- Update documentation when a public workflow or architectural rule changes.
- Update lockfiles when dependencies change.
- Do not mix unrelated cleanup into the same change.

Do not modify generated/runtime state such as controller data directories as source code.

Do not overwrite unrelated user changes.

## 10. When to add an ADR

Add or update an ADR when a change materially affects:

- trust boundaries;
- sandbox isolation;
- network policy;
- credential handling;
- controller persistence ownership;
- lifecycle semantics;
- approval semantics;
- execution architecture;
- Git delivery guarantees;
- major cross-module architecture.

A normal bug fix or localized implementation detail does not need an ADR.

## 11. Verification

Run the narrowest relevant tests while developing.

Before considering a change complete, run the appropriate repository checks.

### Backend

From `backend/`:

```bash
uv sync --extra test --extra dev
uv run ruff check .
uv run pytest
```

Docker integration tests require a running Docker daemon and explicit opt-in. Do not assume they are part of the normal suite.

Four environment variables gate them. Each is off unless set to `1`:

| Variable | Unlocks | Also requires |
|---|---|---|
| `RUN_DOCKER_PREVIEW_TESTS` | preview, sandbox and Git integration tests | a Docker daemon |
| `RUN_DOCKER_DELEGATOR_TESTS` | `tests/delegation/test_docker_integration.py` | a Docker daemon and a real model |
| `RUN_DOCKER_CONTEXT_TESTS` | `tests/implementation_context/test_docker_integration.py` | a Docker daemon and a real model |
| `RUN_DOCKER_HEADLESS_TESTS` | `tests/tasks/test_headless_integration.py` | a Docker daemon and a real model |

For preview, sandbox or Git work, run the gated suite from `backend/`:

```bash
RUN_DOCKER_PREVIEW_TESTS=1 uv run pytest -q
```

This resolves 39 skips into real tests. A change to `app/previews/`, `app/sandboxes/` or
`backend/agent-images/` is not verified by the ungated suite alone, because the gate hides the
tests that exercise it. Gated tests have rotted unnoticed before, precisely because a green
ungated run looked like proof.

The three model-backed gates cost money on every run. Leave them off unless the change is in
that area and you have been asked to run them.

### Frontend

From `frontend/`:

```bash
npm ci
npm run lint
npm test
npm run build
```

A TypeScript change is not complete merely because Vitest passes. The production build must also type-check.

### Cross-stack changes

If a change touches both backend contracts and frontend consumers, verify both sides.

Do not claim a command passed unless you ran it successfully.

If a required check cannot run, state:

- which command was not run;
- why;
- what narrower verification was completed.

## 12. Local development

Backend:

```bash
cd backend
uv sync --extra test
uv run uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

The frontend normally runs on port `5173` and proxies `/api` and WebSocket traffic to the backend on port `8000`.

Build the coding-agent images from `backend/` when planning or coding turns require them:

```bash
docker build \
  -f agent-images/claude/Dockerfile \
  -t orchestrator-agent-claude:latest \
  agent-images/claude

docker build \
  -f agent-images/codex/Dockerfile \
  -t orchestrator-agent-codex:latest \
  agent-images/codex
```

## 13. Dependency policy

Before adding a dependency, check whether the existing stack already solves the problem.

When adding one:

- explain why it is needed;
- prefer maintained and focused packages;
- avoid replacing established infrastructure without a concrete benefit;
- update the correct manifest and lockfile;
- verify the build and tests afterward.

Do not install runtime dependencies only to simplify a test.

## 14. Documentation

Documentation must describe the code that exists, not the intended code.

When behavior changes, check whether these need updates:

- `README.md`
- `backend/README.md`
- `backend/agent-images/README.md`
- `CONTEXT.md`
- `docs/adr/`
- `docs/plans/`

Update `CONTEXT.md` only when canonical domain language or ownership semantics change.

## 15. Explanation style

When explaining this repository:

- Start with the system's purpose and major components.
- Explain the end-to-end flow before individual functions.
- Explain why boundaries exist, not only where code lives.
- Introduce domain terminology gradually.
- Define important terms using `CONTEXT.md`.
- Point to concrete files after establishing the mental model.
- Say which details can be ignored when someone is learning the codebase.

A useful order is:

**mental model → system flow → domain concepts → modules → concrete code → edge cases**

Assume the reader is technically capable but unfamiliar with this repository.

## 16. Definition of done

A change is done when:

- the requested behavior works;
- repository terminology remains consistent;
- relevant safety and lifecycle invariants remain intact;
- tests cover the changed behavior where practical;
- applicable lint, tests, and builds pass;
- documentation matches any changed public behavior;
- no secrets or runtime state were committed;
- the final summary clearly states what changed and what was verified.
