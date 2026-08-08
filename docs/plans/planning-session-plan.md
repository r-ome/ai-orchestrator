# Plan: project-level planning sessions

Companion to `docs/adr/0004-controller-owned-planning-sessions.md`.
Product behaviour lives in `planning-session-design.md`. Implementation detail
lives in `planning-session-spec.md`. This document is the order of work, the
delegation routing, and the exit criteria.

## Goal

A human opens a project and presses **Plan a feature**. A clarifying model asks
progressive questions about outcome, scope, constraints, behaviour, and
trade-offs. It never plans first. When both sides agree, the model states its
understanding and the human confirms it. The controller then sends the agreed
brief to a planner, passes the plan to a reviewer, and lets the two iterate
until the reviewer approves or the review limit is reached. The session ends
with a Plan Spec on screen, ready for a human to read.

## Non-goals for this plan

- **Execution of any kind.** No tech specs, no tasks, no branches, no coding
  agents, no worker assignment, no repository writes. The workflow stops at
  Plan Spec ready.
- Turning a Plan Spec into tasks. That is the next phase and is not designed
  here.
- Streaming model output token by token. Turns are whole; the UI polls.
- Editing a Plan Spec in place. A correction means a new session.
- Cost accounting or token metering per session.

## Decisions already taken

Recorded here so no phase relitigates them.

| Decision | Choice |
|---|---|
| Model transport | Headless CLI in a short-lived container per turn. No LLM HTTP API, no new API key. |
| Conversation memory | The controller stores every turn and replays the transcript. No `--resume`. |
| Planner memory | Persistent. Its transcript accumulates across review rounds. |
| Reviewer memory | Renewed per revision. It receives brief, current plan, and a compact ledger. |
| Repo access | All three roles mount the project volume read-only. |
| Model roles | Env defaults, overridable per session at creation. |
| Credentials | The existing `default` agent credential profile per provider. |
| Session concurrency | Unlimited per project. No partial unique index. |
| Phase advance | The model advises. The human confirms, corrects, or orders it to proceed. |
| Review limit | Default 3 rounds. At the limit a Plan Spec is still produced, marked not approved. |
| Session seed | The human types the feature request in the create dialog. It is message 1. |

## Phases

Each phase ships independently and leaves the system working.

| # | Phase | Routing | Why that model |
|---|---|---|---|
| 0 | Store schema, status machine, session models | `deep` (Opus, medium) or Codex Terra high | A new state machine with a review loop; wrong transitions strand sessions |
| 1 | Model runner and prompt contracts | `worker` (Sonnet 5, medium) or Codex Terra high | Contained module, exact CLI flags supplied, easy to verify with a stub |
| 2 | Clarify phase: service, router, background turns | `worker` (Sonnet 5, medium) or Codex Terra high | Standard FastAPI work against a specified store API |
| 3 | Plan and review loop, ledger, Plan Spec | `deep` (Opus, medium) or Codex Terra high | The ledger and the loop exit conditions are where a wrong answer is expensive |
| 4 | Frontend API client and project section | `worker` (Sonnet 5, medium) or Codex Terra medium | Mirrors an existing section component |
| 5 | Frontend session page and route | `worker` (Sonnet 5, medium) or Codex Terra medium | New page, conventions already set by phase 4 |

Phases 0 and 1 are independent and may run in parallel. Phase 2 depends on 0
and 1. Phase 3 depends on 2. Phase 4 depends on 2. Phase 5 depends on 3 and 4.

**Recommended delegation batches**

1. Batch A, parallel: phase 0 and phase 1.
2. Batch B: phase 2.
3. Batch C, parallel: phase 3 and phase 4.
4. Batch D: phase 5.

## Running phases concurrently

Parallel phases share one working tree, so ownership is by file, not by phase.
Give each agent its owned-files list from the spec and tell it which files
other live agents hold. The same two rules from `task-preview-plan.md` apply:

- **No tree-wide git commands.** No `git add -A`, no `git checkout .`, no
  `git stash`. This is the one mistake that can destroy another agent's work.
- **Report, do not fix, failures in files you do not own.** A failing test in
  another phase's in-flight file is almost always that phase mid-write.

Only phase 0 and phase 3 touch `backend/app/controller/store.py`, and they never
run at the same time. Only phase 4 and phase 5 touch `frontend/src/App.tsx` and
`frontend/src/pages/ProjectDetailPage.tsx`; phase 5 waits for phase 4.

## Exit criteria

**Phase 0.** `ControllerStore.initialize()` creates the four planning tables on
a fresh database and on an existing one, and records migration version 7. Every
transition in `PLANNING_TRANSITIONS` is reachable and every non-terminal status
has an exit. A guarded status update from a wrong source status changes no row
and returns `False`. `claim_turn` succeeds once and then returns `False` until
the turn is released. Tests run without Docker.

**Phase 1.** `run_planning_turn` builds the exact documented command for each
provider, parses a well-formed JSON envelope, retries once on malformed output,
and raises a typed error on a second failure, on a non-zero exit, and on
timeout. The container is created with the project volume read-only and with
networking enabled. Tests use a stub Docker client and run without Docker.

**Phase 2.** Creating a session from the project page stores the human's
request as message 1, sets `clarifying`, and runs one clarifier turn in the
background. Polling the session shows `turn_state` moving `running` to `idle`
and the clarifier's questions appended. Posting a second message while a turn
runs returns 409. `proceed` and `confirm` move the session to `planning`.
`cancel` settles any non-terminal session. A backend restart during a turn
leaves that session `failed`, never `running`.

**Phase 3.** A confirmed session produces a planner revision, a reviewer
verdict, and either `plan_ready` or another round. The planner prompt for round
2 contains its own round-1 plan and the reviewer's findings. The reviewer prompt
for round 2 contains the brief, the round-2 plan, and the ledger, and does not
contain the round-1 planner transcript. Findings keep stable ids across rounds.
At `max_review_turns` the session reaches `review_limit_reached` and still
writes a Plan Spec whose `reviewer_outcome.approved` is `false` and whose
outstanding findings are listed. No repository file is written in any path.

**Phase 4.** The project page shows a Planning section with a **Plan a feature**
primary action and a session list with status pills. The create dialog takes a
title, a feature request, and optional provider overrides. Creating a session
navigates to its page.

**Phase 5.** The session page shows status, the conversation, a composer, the
Confirm, Correct, Proceed anyway, and Cancel actions in the right states, review
round progress, and the Plan Spec as a summary with the full document
expandable. The page polls only while the session is non-terminal.

## Verification

Backend tests live in `backend/tests/planning/`. Every backend phase adds cases
there. Run:

```
cd backend && uv run pytest
```

Verify a delegated agent's reported test count with `pytest --collect-only -q`.

Frontend type checking and lint:

```
cd frontend && npm run build && npx oxlint
```

Manual end-to-end check needs a logged-in CLI in the `default` credential
profile for whichever providers the session uses, and a project whose copy has
finished. The `personal-blog-sandbox-1` sandbox is a suitable target.

## Known consequences to accept

- **Every turn costs a container start plus model inference.** Turn latency in
  this setup is unmeasured. A ten-question clarification is ten container
  cycles. If that proves too slow, the fix is a warm per-session container, not
  a change to the state machine.
- **Transcript replay makes prompts grow.** Each clarifier turn resends the
  whole conversation, and the planner transcript grows with every review round.
  There is no provider-side prompt caching across containers. The reviewer is
  the one role held flat, by the ledger.
- **Planning consumes the same provider subscription as coding agents.** A long
  planning session and a running coding agent draw on one credential profile
  and can hit the same rate limit.
- **A read-only workspace mount can break a CLI that expects to write to its
  working directory.** The spec gives the fallback: run with the working
  directory in tmpfs and grant read access to `/workspace` instead.
- **Planning containers need network access**, unlike the git and inspection
  helpers, which run with `network_disabled=True`. They reach the model
  provider. They cannot reach the project volume for writing, which is the
  boundary that matters.
- **A model can still assert a false understanding.** Confirmation proves the
  human agreed with the summary shown, not that the summary was complete. The
  full transcript is kept so a bad plan can be traced to the turn that caused
  it.
- **Cancelling does not stop a running container.** The session settles
  immediately and the background turn's output is discarded when it lands. The
  container runs to completion or times out.
- **Sessions are never deleted in this phase.** A project accumulates every
  session it has ever started.
