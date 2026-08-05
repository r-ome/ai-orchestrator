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

**Preview proposal**:
A non-executable suggestion for running the current sandbox, including protected-file changes and editable settings.
_Avoid_: Detection result, preview configuration

**Approval**:
Human permission to execute one exact preview proposal revision.

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
