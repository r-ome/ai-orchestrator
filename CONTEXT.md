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

**Turn**:
One model invocation in one short-lived container.

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
One database container serving every sandbox of a project. Each sandbox holds its own schema unless it is a database guest.
_Avoid_: Shared database, common database

**Database guest**:
A sandbox that writes to another sandbox's schema. It never owns that data, never migrates or seeds it, and loses only its own credentials when it stops.
_Avoid_: Attached sandbox, linked sandbox

**Trusted metadata**:
Controller-owned workflow and audit state that coding agents and preview stacks cannot modify.
_Avoid_: Sandbox metadata
