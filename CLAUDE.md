# CLAUDE.md

This repository uses `AGENTS.md` as the shared source of truth for coding-agent behavior.

Before doing substantive work:

1. Read `AGENTS.md`.
2. Read `CONTEXT.md`.
3. Inspect the relevant implementation and tests.
4. Read relevant ADRs or plans when the change affects architecture or an existing design.

Follow `AGENTS.md` unless this file gives a Claude-specific instruction.

## Working approach

Do not start coding from the user's description alone when the task involves existing behavior.

First establish:

- which domain owns the behavior;
- how the current flow works;
- which state is authoritative;
- which tests exercise it;
- whether an approval, isolation, Git, credential, or persistence invariant is involved.

For non-trivial work, think through the implementation before editing.

Prefer a small coherent solution over broad cleanup.

Do not introduce abstractions for hypothetical future requirements.

## Repository mental model

Orchestrator is a control plane around code-writing agents.

The main flow is:

```text
Human
  |
  v
React control panel
  |
  v
FastAPI controller
  |
  +--> planning / review
  +--> delegation / tasks / turns
  +--> Docker sandboxes and previews
  +--> Git repositories
  `--> controller-owned SQLite
```

Remember the ownership boundaries:

- Git and sandbox files represent code state.
- Docker represents runtime state.
- Controller SQLite represents trusted workflow and approval state.
- Coding agents may change sandbox code.
- Coding agents must not control trusted controller state.

Use the exact domain language from `CONTEXT.md`.

## Safety-sensitive code

Take extra care in:

```text
backend/app/agents/
backend/app/controller/
backend/app/delegation/
backend/app/planning/
backend/app/previews/
backend/app/sandboxes/
backend/app/tasks/
backend/app/turns/
backend/agent-images/
```

Before changing these areas, identify the invariant the existing code protects.

Never weaken a security or approval boundary merely to make a workflow easier.

In particular, preserve:

- explicit preview approval;
- exact-state review;
- protected runtime-file validation;
- sandbox isolation;
- credential-volume separation;
- fast-forward delivery guarantees;
- controller-owned trusted metadata;
- explicit confirmation for destructive operations.

## Code changes

When editing code:

- follow nearby patterns;
- modify the owning module instead of adding unrelated global helpers;
- keep API, domain logic, and infrastructure concerns separated;
- avoid drive-by refactors;
- preserve existing persisted-state compatibility when relevant;
- update tests alongside changed behavior;
- update docs when the user-visible workflow or architecture changes.

If you encounter suspicious existing code outside the requested scope, mention it rather than silently rewriting it.

Do not modify unrelated user changes.

## Backend

The backend is Python 3.11+ with FastAPI, pytest, and Ruff.

For backend work, normally verify with:

```bash
cd backend
uv sync --extra test --extra dev
uv run ruff check .
uv run pytest
```

Do not run Docker integration suites unless the environment supports Docker and the relevant opt-in is appropriate.

## Frontend

The frontend is React + TypeScript + Vite.

API access belongs in `frontend/src/api/`.

Reusable UI belongs in `frontend/src/components/`.

Route-level workflow composition belongs in `frontend/src/pages/`.

Tests use Vitest and Testing Library and normally live beside the implementation.

Verify frontend work with:

```bash
cd frontend
npm ci
npm run lint
npm test
npm run build
```

## Browser verification

The coding-agent images already contain pinned Playwright and Chromium.

Use the image-provided browser tooling for behavioral verification when available.

Do not add Playwright, Chromium, or another browser dependency to the target project solely for agent verification.

## Communication style

Write in ASD-STE100-inspired plain language.

- Prefer one main idea per sentence.
- Use active voice.
- Use the same term for the same concept.
- Define unavoidable jargon when first introduced.
- Be concise but complete.
- Remove repetition and routine narration.
- Use tables, steps, diagrams, or code blocks only when they improve understanding.

When explaining architecture, start broad and then become concrete:

**big picture → system flow → terminology → relevant modules → concrete code**

Explain why an architectural boundary exists, not only what file implements it.

## Planning and uncertainty

Do not present guesses about repository behavior as facts.

If behavior is unclear:

1. inspect the implementation;
2. inspect its callers;
3. inspect its tests;
4. inspect the relevant ADR or documentation.

Prefer evidence from the current code over assumptions from common framework conventions.

If requirements are underspecified but a safe, reversible interpretation is clear, proceed with that interpretation and state it.

Ask the user only when different interpretations would materially change behavior or architecture.

## Completing work

Before reporting completion:

1. Review the diff for unintended changes.
2. Run the relevant verification commands.
3. Check for missing tests.
4. Check whether documentation became stale.
5. Check that no secrets, credentials, runtime data, or temporary files entered the diff.

In the final response, state:

- what changed;
- important design decisions;
- tests or checks run;
- anything that remains unverified.

Never say that tests passed unless you actually ran them.
