# Plan: task branches and gated previews

Companion to `docs/adr/0002-previews-run-from-sandbox-commits.md` and
`docs/adr/0003-controller-owned-dependency-volumes.md`.
Implementation detail lives in `task-preview-spec.md`. This document is the
order of work, the delegation routing, and the exit criteria.

## Goal

An agent edits a sandbox. The human sees the change immediately in a live
preview. When the agent reports the task done, the controller builds an
immutable preview from the resulting commit. The human accepts or rejects.
Accept merges into the sandbox branch. Reject deletes the branch.

## Non-goals for this plan

- Concurrent tasks per sandbox. One task at a time. `git worktree` isolation
  is deferred until the single-task flow is proven.
- Garbage collection of dependency volumes. Tracked, not built here.
- Any change to the compose or dockerfile preview modes. Native mode only.

## Phases

Each phase ships independently and leaves the system working.

| # | Phase | Routing | Why that model |
|---|---|---|---|
| 0 | Git baseline in the sandbox | `worker` (Sonnet 5, medium) | Mechanical, well-specified, easy to verify |
| 1 | Dependency volume keyed by lockfile | `worker` (Sonnet 5, medium) | Contained change to two call sites |
| 2 | Task records and task branches | `deep` (Opus, medium) | New state machine; wrong transitions are expensive |
| 3 | Preview from a commit | `worker` (Sonnet 5, medium) | Replaces one function, spec is exact |
| 4 | Accept and reject | `deep` (Opus, medium) | Merge conflicts and data loss live here |
| 5 | Timing and live logs | `worker` (Sonnet 5, medium) | Additive; existing event store does the work |
| 6 | Metadata move and container limits | `quick` (Haiku) | Constant moves and keyword arguments |

Phases 0 and 1 are independent and may run in parallel. Phase 2 depends on 0.
Phase 3 depends on 2. Phase 4 depends on 3. Phases 5 and 6 depend on nothing.

## Running phases concurrently

Parallel phases share one working tree, so ownership is by file, not by
phase. Give each agent an explicit owned-files list and tell it which files
other live agents hold. Two rules make this safe:

- **No tree-wide git commands.** See the prohibition in the spec's rules
  section. This is the one mistake that can destroy another agent's work.
- **Report, do not fix, failures in files you do not own.** A failing test in
  someone else's in-flight file is almost always their work mid-write. Two
  agents wasted effort here: one "fixed" a migration assertion that was
  correct, another diagnosed a regression that was another phase landing.

Re-run the suite before concluding anything, and verify an agent's reported
test counts with `pytest --collect-only -q` — one phase overstated its count
by six.

## Exit criteria

**Phase 0.** Every sandbox volume holds a git repository with at least one
commit. A sandbox created from a host folder that is not a repository still
gets one. `git log` in the agent container shows the baseline commit.

**Phase 1.** A second preview start for an unchanged lockfile reuses the
existing dependency volume and reports zero install duration. The agent
container can run the project build without a network install.

**Phase 2.** Starting a task creates a branch. The agent commits to it. The
controller reads the head commit without trusting any file the agent wrote.

**Phase 3.** A task preview serves the code at the task commit. Restarting it
serves the same code. The live preview reflects an agent edit within two
seconds and without a restart.

**Phase 4.** Accept fast-forwards the sandbox branch and leaves no task branch.
Reject leaves the sandbox branch untouched. Neither path can lose committed
work. A conflicting accept fails with a clear error instead of a partial merge.

**Phase 5.** Clone, install, build, and preview start each emit a duration in
milliseconds. A client can stream those events live.

**Phase 6.** No controller metadata is reachable from the agent workspace. The
agent container carries a pids limit and a memory limit.

## Verification

Backend tests live in `backend/tests/`. Every phase adds cases there. Run:

```
cd backend && uv run pytest
```

Manual check for phases 3 and 4 uses the `personal-blog-sandbox-1` sandbox,
which already reproduces the original failure: an agent-created
`src/pages/contact.astro` that the preview never served.

## Known consequences to accept

- Git ignore rules now decide what reaches a task preview. The old tar copy
  used its own exclusion list. A file the project gitignores will not appear
  in a task preview even though it appears in the sandbox.
- The live preview writes build output back into the sandbox volume. Overlay
  `dist/` and `.astro/` with run-scoped volumes if that becomes a problem.
  This interacts badly with the dirty-tree rule: `git status --porcelain`
  hides ignored files, so a project that does not gitignore its build
  directory will reject every completion report on untracked build artifacts.
  Correct by the rule as written, but it will read as a bug. Phase 3 should
  overlay those paths rather than rely on each project's `.gitignore`.
- Dependency volumes accumulate one per distinct lockfile per sandbox.
- A live preview's install job runs against the sandbox itself, not a copy, so
  the post-install protected-file check can now fire. If `npm install`
  rewrites `package-lock.json`, the first start aborts with "Dependency
  installation changed protected runtime files". It self-heals, because the
  sandbox keeps the rewritten lockfile and the next start passes. The check
  was kept rather than weakened: the lockfile rewrite is a real change to the
  sandbox and the approval genuinely no longer holds.
- `.env` and `.env.local` are added to each sandbox's `.git/info/exclude`, so
  a coding agent committing one is invisible to `git status --porcelain`. Env
  files were already excluded from previews by design, so an agent's `.env`
  was never a reviewable task result.
- Live previews mask env files at the root unconditionally, but only at
  deeper paths that exist when the preview starts. See the spec's section 5
  limitation note.
