# Orchestrator

Orchestrator creates isolated coding workspaces and controlled application previews from host project folders.

## Language

**Sandbox**:
An editable snapshot of one project folder with a stable identity and dedicated project volume.
_Avoid_: Project, workspace, copy

**Coding agent**:
The single writable automation session assigned to a sandbox.
_Avoid_: Worker, bot

**Reviewer**:
A read-only participant that can inspect a sandbox without changing it.

**Planning session**:
One attempt to turn a feature request into a reviewed plan for a project.
_Avoid_: Planning run, plan thread

**Clarifier**:
The model that questions the human until the feature is understood and never writes a plan.
_Avoid_: Main model, interviewer

**Planner**:
The model that turns the agreed feature brief into a proposed implementation plan across review rounds.

**Plan reviewer**:
The model that judges one plan revision, separate from the read-only sandbox **Reviewer**.
_Avoid_: Reviewer, critic, checker

**Feature brief**:
The controller-assembled document frozen when the human confirms the feature understanding.
_Avoid_: Requirements, prompt

**Plan revision**:
One numbered planner output.

**Finding**:
One reviewer objection to a plan revision with a stable id, severity, status, and planner response.

**Review ledger**:
The compact per-session record of every unresolved finding and its current state.

**Plan Spec**:
The final planning-session document with scope, approach, risks, open questions, and reviewer outcome.

**Implementation context**:
The controller-assembled description of one sandbox's repository: its manifest, its
resolved commands, and its inventory. Built once per planning session before delegated
work starts.
_Avoid_: Repo scan, project context, codebase map

**Delegation**:
One attempt to implement an approved plan in one sandbox. It owns the work items and
reads one **Implementation context**.
_Avoid_: Implementation run, execution, rollout

**Work item**:
One numbered unit of an approved plan, with its own objective, scope, write scope,
acceptance criteria, and verification intents.
_Avoid_: Ticket, subtask, story

**Work item run**:
One numbered attempt at one **Work item**. A work item may have several; only the
retained ones decide its state.
_Avoid_: Retry, execution, job

**Task**:
One coding-agent turn on a temporary branch cut from a **Sandbox**. The controller cuts
the branch, the agent commits to it, the controller verifies the branch against git, and
then fast-forwards it (accept) or deletes it (reject).

A Task belongs to a **Sandbox**, not to a **Delegation**. Three things open one: a **Work
item run**, a **Feature change request**, or a direct request against the sandbox. Each
sandbox may hold only one open Task at a time.
_Avoid_: Job, ticket, work item, sandbox change

**Verification**:
One run of the commands a **Work item** declared, executed once each in a **Hardened
run** that invokes no model, stopping at the first failure. Also the evidence that run
records.
_Avoid_: Test run, check, CI

### How they nest

Ownership as the schema stores it. A **Task** hangs off the sandbox, and the delegation
side points at it — it is not owned by the delegation chain.

```text
Planning session
  ├── Implementation context   (session, sandbox)
  └── Delegation               (session, sandbox, context)
        ├── Work item
        │     └── Work item run ──────── task_id ──┐
        └── Feature change request ───── task_id ──┤
                                                   │
Sandbox                                            │
  └── Task  ◄──────────────────────────────────────┘
        also opened directly against the sandbox, with no delegation
```

**Hardened run**:
One execution of one command in one short-lived container under the security
boundary ADR-0006 sets. The boundary is a constant of the run, never an argument
to it.
_Avoid_: job, exec, container run

**Turn**:
One model invocation in one short-lived container.
A Turn is a **Hardened run** that invokes a model. A verification command is a
**Hardened run** that does not.

**Validated turn**:
One **Turn** whose payload is checked against its role's contract, with exactly
one repair attempt if the check fails. It yields an outcome, not an exception:
an invalid payload is a result, while a failed container is still an error.
_Avoid_: Retry, repair loop, turn with repair

**Preview proposal**:
A non-executable suggestion for running the current sandbox, including protected-file changes and editable settings.
_Avoid_: Detection result, preview configuration

**Approval**:
Human permission to execute one exact preview proposal revision.

**Feature review**:
One model verdict pinned to the exact base and head commits for a completed delegation.

**Feature change request**:
A human instruction that updates the complete implementation after delegated work ends.
It remains awaiting review until the current whole-feature review approves it.
_Avoid_: Work item, fix task

**Awaiting review**:
The state of an incorporated feature change that has passed controller checks but lacks approval from the current whole-feature review.
_Avoid_: Completed, accepted

**Source merge**:
An explicit fast-forward of an approved feature commit into the original project folder.
It refuses a changed branch, a changed commit, or an uncommitted worktree.
_Avoid_: Sync, copy back, publish

**Preview stack**:
The active application runtime for a sandbox, containing one or more related containers.
_Avoid_: Preview container

**Protected runtime file**:
A project file whose change invalidates earlier preview approval.
_Avoid_: Protected file

**Shared database server**:
One database container serving every sandbox of a project. Each sandbox holds its own schema.
_Avoid_: Shared database, common database

**Database guest** (historical):
A sandbox that writes to another sandbox's schema. Every managed sandbox owns its schema, so no new guest can be created. The term names rows written before that rule, which lose only their own credentials when the sandbox stops.
_Avoid_: Attached sandbox, linked sandbox

**Trusted metadata**:
Controller-owned workflow and audit state that coding agents and preview stacks cannot modify.
_Avoid_: Sandbox metadata
