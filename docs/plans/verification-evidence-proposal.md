# Proposal: verification evidence without polluting the repository

**Status:** proposed, not implemented
**Date:** 2026-08-14
**Decision owner:** Jerome

## The problem, stated once

A delegated feature change must prove it works. Today the only proof the review
gate accepts is a file committed to the repository. So an agent verifying an
interactive UI change has to commit its test harness, and that harness ships in
the pull request alongside the feature.

We want the opposite. A pull request should contain the feature, and genuine
tests when the project has a suite to put them in. A throwaway browser script
the agent wrote to convince itself is not a repository change.

## What actually happened

This is not hypothetical. One feature — a copy-link button on a personal blog —
took four change-request turns and four review rounds to merge, and produced a
PR containing a 350-line Playwright harness the project never wanted.

| Turn | What the agent did | Outcome |
|---|---|---|
| 1 | Wrote `/tmp/verify_action_bar.js`, ran it against live Chromium, 11/11 passed, deleted it, committed nothing | **Failed.** `Task branch has no commit beyond <sha>` |
| 2 | Committed the harness as `scripts/verify-action-bar.mjs` plus a `playwright` devDependency | Rejected. Lockfile not regenerated; acceptance criteria under-reported |
| 3 | Reverted the devDependency, re-ran using the sandbox's Playwright, 22/22 passed | Rejected. Its own report said "no install of any kind", which matched the substring `" install"` |
| 4 | — | Approved, after two validator fixes and a data backfill |

**Turn 1 did what we want.** It verified properly and kept the repository clean.
The system failed it for that.

## The three rules that combine badly

1. **A turn must commit.** `backend/app/tasks/service.py:491` fails any turn
   whose branch head equals its base. A verification-only turn is impossible.
2. **The reviewer wants evidence in the repository.** Its rejections named "no
   browser-based or automated runtime check exists anywhere in the repo". The
   only way to satisfy that is to commit the harness.
3. **Installing test infrastructure is forbidden.** `_installs_test_infrastructure`
   in `backend/app/delegation/change_requests.py` rejects it, and
   `_is_behavior_check` disqualifies any check that looks like one.

Rule 3 is right and should stay. Rules 1 and 2 are what force the harness into
git. Note that rule 2 was never stated to the agent — it inferred it from
rejection text — while rule 3 was never stated either, so an agent following the
reviewer's reasoning walks straight into it.

## Proposed contract

> Verification runs for real, outside the worktree. The evidence of record is
> the run — command, exit code, output — not a file in the repository. An agent
> commits a test only when it belongs to a suite the project already has.

Four changes follow.

### 1. A verification-only turn may settle without a commit

`tasks/service.py` should distinguish "the agent failed to make a required
change" from "the agent judged no change was needed and proved it". The second
settles as completed, carrying its evidence.

Guardrail: it settles only when the turn reports a passing verification. A turn
that commits nothing and verifies nothing still fails.

This also retires the opaque `has no commit beyond <sha>` for the common case.

### 2. The reviewer accepts a recorded run

`_change_evidence_findings` and the review prompt must treat a recorded
verification run as evidence, and must stop asking for repository-resident
artifacts. The reviewer should be told what the sandbox provides — Playwright is
already available via `NODE_PATH` — so it stops citing absent tooling as a
finding.

### 3. The agent is told the rule instead of inferring it

The change-request prompt should state plainly: write verification harnesses
outside the worktree; never commit one; never install browser tooling; commit a
test only when the project already has a suite that test belongs to.

All three constraints exist today. None are stated. Every violation this feature
hit came from an agent guessing.

### 4. "Has a test suite" needs a definition

Rule: a project has a suite when its manifest declares a test runner, or a
conventional test directory exists. Absent that, the agent commits no test
files. This wants pinning down before implementation — see open questions.

## What this does not change

- The install ban stays.
- Behavioral changes still require behavioral verification. The standard does
  not drop; only the artifact does.
- The review gate still blocks publish. `reviewed_target` is unchanged.

## Open questions

1. **Trust.** If evidence is a recorded run rather than a committed file, what
   stops an agent reporting a run it did not perform? Today nothing does —
   `ToolCall` (`backend/app/tasks/runner.py:60-62`) records only a tool name and
   a failure flag, never the command text. The structured record in
   `verification["commands"]` covers only the implementation context's confirmed
   commands, which for this project was `npm run build` alone.

   Two ways to close it, neither free:
   - Route the behavioral check through the verification runner, so the system
     executes it and records the result. Requires the check to be a declared
     verification command, which is a data change per project.
   - Extend `ToolCall` to carry command text, touching turn parsing for both
     Claude and Codex event streams.

   **This is the substantive decision.** Without it, change 2 weakens the gate:
   the reviewer would trust a self-report it cannot check.

   **Decided 2026-08-14 (Jerome): route through the verification runner.** The
   system executes the check and records it, so the agent never self-reports.
   See "What the runner imposes" below — the route is sound but the obvious
   implementation does not work.

## What the runner imposes

Reading `backend/app/delegation/verification.py` and
`backend/app/implementation_context/inventory.py` against the decision above.

Each verification command runs in a **fresh container**: rootfs `read_only`,
`/workspace` mounted rw from the sandbox volume, `/tmp` a per-container tmpfs,
`network_mode="none"` (loopback resolves, no egress), `entrypoint=[]`, and the
command produced by `shlex.split`. Consequences:

- **No shell.** `sh -lc "start app && run browser"` is not available; the
  command is an argv executed directly. A behavioural check must therefore be a
  single self-contained program.
- **`/tmp` is not shared.** A harness the agent writes to `/tmp` during its turn
  is gone before the runner starts. Only `/workspace` survives, which is the
  worktree.
- **The command is not free text.** `confirm_command` accepts only a known
  program: an npm-family runner naming a script `package.json` declares, a
  Makefile target, a Python tool or runner, `cargo`, or `go`. Plain `node` is
  not on that list. `COMMAND_KINDS` is closed at
  `("build", "test", "lint", "typecheck", "format")` — there is no behavioural
  kind.

**So the obvious implementation is self-defeating.** The only confirmable way to
run a browser harness is `npm run <script>`, which requires a script line in
`package.json` pointing at a harness file — putting the harness back in git,
which is the problem this proposal exists to solve.

**Recommended shape instead:** a `behavior` command whose text is a *system
constant*, not agent-authored. The controller runs its own entrypoint against a
harness the agent leaves at a conventional untracked path under `/workspace`.
The trust property holds — the system still chooses and records the command —
and the repository stays clean. This bypasses `confirm_command` deliberately,
because there is nothing agent-supplied left to confirm.

### The image is fine — measured, 2026-08-14

`get_verification_settings` (`backend/app/delegation/config.py:81-88`) defaults
`DELEGATION_VERIFICATION_IMAGE` to `TASK_GIT_IMAGE`, and that to
`orchestrator-agent-claude:latest`. The runner and the agent share one image, so
what the sandbox provides, the runner provides. Probed directly against that
image under the runner's full constraint set — `read_only`, `cap_drop ALL`,
`no-new-privileges`, `network none`, `pids_limit 512`, `mem 2g`, tmpfs `/tmp`:

- `playwright` is at `/usr/local/lib/node_modules/playwright`, on `NODE_PATH`.
- Browsers are baked in at `/ms-playwright` (`chromium-1234`,
  `chromium_headless_shell-1234`, `ffmpeg-1011`). No download needed, which
  matters because the runner has no egress.
- Chromium **launches and drives a page**: `PLAYWRIGHT_LAUNCH_OK`.
- A harness may **start its own server and browse it over loopback** in the same
  container: served `127.0.0.1:5173`, read the button text back, `LOOPBACK_OK`.

So the route needs no image change, and the "one self-contained program"
constraint is satisfiable: the harness starts the app and drives the browser
itself.

**One trap found.** `NODE_PATH` applies to CommonJS `require` only. An ESM
harness fails with `ERR_MODULE_NOT_FOUND: Cannot find package 'playwright'`.
Harnesses must be `.cjs` using `require`, or import by absolute path. The
original failing harness was `verify_action_bar.js` — worth checking whether
this trap contributed. Whatever prompt tells the agent to write a harness must
state it.

2. **Detecting a test suite** reliably across project types.

3. **Where harnesses live.** ~~`/tmp` inside the sandbox works~~ — **answered by
   the decision above, and the answer is no.** Once the runner executes the
   check, `/tmp` is the wrong home: the runner gets a fresh tmpfs and cannot see
   what the agent's turn wrote. A harness must sit at an untracked path under
   `/workspace`.

## Related defects found alongside this

Fixed, uncommitted at time of writing:

- `_installs_test_infrastructure` matched `" install"` inside prose such as
  "no install of any kind", so an agent that correctly installed nothing was
  recorded as having installed, and its real browser run stopped counting.
- `_change_evidence_findings` judged every change request still at
  `awaiting_review`, so one weak turn blocked a delegation permanently.

Unfixed:

- `ChangeRequestStatus.COMPLETED` (`backend/app/delegation/models.py:35`) is
  declared and never used. No change request ever reaches a terminal success
  state. Change 1 above should set it.
- The orchestrator writes `Co-Authored-By` trailers into the commits it
  generates, against project convention.
