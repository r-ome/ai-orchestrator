# Proposal: feature sandbox architecture

**Status: Proposed. Not accepted. No ADR yet.**

This document exists for review. It does not authorize any change. If it is
accepted, the decisions belong in `docs/adr/` and the execution order belongs in
a separate plan document.

## Provenance

This is a merge of two independent reviews of the same target architecture,
written against commit `34b6040`, plus a third pass that verified the disputed
claims against the code. Section 2 records every claim that one review got wrong
and the other caught, because several of them change the plan.

The database engine decision is separable and can be accepted or rejected on its
own. It is section 8.

Anchors were re-verified. Line numbers drift; function names are authoritative.

---

## 1. Target being reviewed against

One project holds many independent feature sandboxes. Docker stays the isolation
layer. Each sandbox owns a container, a persistent workspace volume, an
independent Git clone, a feature branch, a pinned base commit, an isolated
database, and isolated runtime and network configuration.

Shared resources are immutable or expensive: Git object data, package caches,
browser binaries, base images, a shared database server. Mutable state stays
isolated: workspace, Git refs, database, processes, runtime configuration.

No shared writable `.git`. No Git worktrees between sandboxes. No automatic sync
when main moves. Resource names derive from the sandbox ID. A manifest records
intent, written before resources are created. Lifecycle operations are
idempotent.

Docker is blast-radius isolation against accidental destructive agent actions,
not a hardened boundary against an adversary. GitHub credentials and publish
actions stay controller-owned.

---

## 2. Corrections register

Both source reviews contained errors. These were verified directly.

| Claim | Verdict | Consequence |
| --- | --- | --- |
| "The SQLite sandbox row is created lazily and later" | **Wrong.** `projects/router.py:83` calls `register_sandbox` immediately after `register_project` returns | Registration is *late*, not lazy. The ordering defect is real; the description was not |
| "`helper.start()` is outside the try that rolls back" | **Wrong.** It is inside, at `projects/service.py:246`, with `_rollback_registration` on both `except` paths | Ordinary Docker exceptions do roll back. Only a process crash orphans resources |
| "Dependency volumes are mounted `ro` into preview containers" (repeats ADR 0003) | **Wrong.** `previews/service.py:1124`, `:1128`, `:1205` mount them `rw`. The comment at `:1200` explains why — Vite writes `node_modules/.vite` | Only the *agent* mount is `ro` (`agents/service.py:176`). ADR 0003's summary overstates the invariant |
| "MySQL 8.4 only" | **Too strong.** `PreviewDependencyService.image` is configurable | Accurate form: MySQL *protocol* only. The image is not pinned |
| "Each sandbox has one long-lived agent container" | **Too strong.** Agent containers are optional, randomly named, and auto-removed | A sandbox often owns no container at all |
| `uuid5(NAMESPACE_URL, …)[:12]` | **Invalid Python.** `TypeError: 'UUID' object is not subscriptable` | Correct form is `.hex[:12]` |
| "`git clone --local` hardlinks are safe because Git objects are immutable" | **Insufficient.** True of Git's own behaviour, irrelevant to an agent | See 2.1 |
| "Destroy-and-recreate gives the same guarantee as blue-green sync" | **Wrong.** Recreation loses unpushed commits, workspace changes, and database state | See 2.2 |
| "Steps 1 to 3 are behaviour-preserving" | **Wrong.** There is no migration runner. See 2.3 | Reorders the whole plan |
| "New sandboxes should be Postgres-only, MySQL legacy" | **Rejected.** Per-project engine detection was already chosen | See section 8 |
| "The companion database document is absent" | **Correct at the time.** It was missing from the working tree during that review, was restored, and is now folded into section 8 | No separate file remains |
| "Sync aborts and restores on failure" | **Wrong.** A Git safety ref cannot undo migrations or seeds | Rewritten as 5.4.1. Sync is Git-reversible only |
| "Lifecycle can reuse `sandboxes.status`" | **Wrong.** `register_sandbox` overwrites it unconditionally from five call sites | Separate `lifecycle_status` column (5.3) |
| "Strip remotes, then sync fetches `origin/main`" | **Contradiction.** The stripped clone has no `origin` | Two-step canonical fetch and sandbox import (5.5) |
| "GitHub credentials are needed only at publish" | **Wrong.** Private repositories need read credentials at create and sync | Read/write credential split (5.6) |
| "`.git/info/exclude` protects the SQLite database" | **Insufficient.** It suppresses only untracked files | Relocate plus refuse tracked database paths (8.6) |
| "28 MySQL references" | **Wrong.** 37 lines match case-insensitively | Corrected in 8.1 |
| "Per-sandbox networks conflict with preview cleanup" (raised against this proposal) | **Not confirmed.** `_preview_networks` filters on run ID as well as managed, so sandbox networks are never discovered | Ownership rule stated explicitly in 5.2 |
| "48-bit identity is a blocking gap" (raised against this proposal) | **Math correct, scale premise unrealistic.** 0.18% at one million, 1.8e-7% at one thousand | Full hex adopted anyway; it costs nothing (5.2) |
| "Linked worktrees silently replace history" | **Wrong.** Measured: Git 2.50.1 exits 128, preserves the `.git` file, and `set -eu` halts the script | Rewritten in 3.2. A fail-closed compatibility defect, not data loss |
| "A writer predicate is enough" | **Wrong.** Four partial indexes on four tables cannot express cross-class exclusion, and a read-then-act check races. `delegation/execution.py:163` starts a task with no writer check | Admission gate, not predicate. Delegations added as a fourth writer class (5.4.2) |
| "`reset-db` recovers a failed sync" | **Incomplete.** It rebuilds the database but leaves `current_base_commit` naming the old base | `pending_base_commit` plus a finalizing reset (5.4.1) |
| "`awaiting_engine_confirmation` covers engine detection" | **Incomplete.** A state with no transition out of it is terminal | `confirm-engine` operation plus controller-owned migration commands (5.4.3) |
| "Additive `remote_url` gives remote-based project identity" | **Wrong.** `source_path` is `NOT NULL UNIQUE` and drives the upsert at `store.py:520` | Normalization, credential stripping, nullability, and a separate v1 store path (5.3) |
| "Excluding the SQLite file from Git is sufficient" | **Wrong.** `git archive` then omits it from task previews, which provision an ephemeral database instead | Sandbox-owned `sbx-<id>-db` volume outside the workspace (8.6) |
| "One exclusive admission gate for all writers" | **Wrong.** Writers nest — a delegation starts a task (`execution.py:163`), a task preview needs an open task (`previews/service.py:261`). Both children would deadlock | Split into an exclusive lifecycle lease and nesting writer sessions (5.4.2) |
| "Step 1 is additive for `projects`" | **Wrong.** `source_path` is `NOT NULL UNIQUE`; SQLite cannot drop that with `ALTER TABLE` | Table rebuild required, called out in section 6 |
| "Destroy stops writers, and the lease refuses while writers exist" | **Contradiction.** Destroy could never acquire its own lease | Draining state added (5.4.2) |
| "The lease covers the four destructive operations" | **Too narrow.** `create`, `resume`, and `confirm-engine` also provision and repair resources | Lease covers every lifecycle mutation, released across human waits (5.4.2) |
| "Check for a lease, then proceed under the existing index" | **Not atomic.** Previews create Docker resources before their row (`:443` vs `:544`); delegations become active at revision creation (`service.py:107`) | Check and insert share one transaction; preview row moves ahead of Docker (5.4.2) |
| "Only preview and delegation admission need reordering" | **Wrong.** `start_task` runs `ensure_git_baseline` before `create_task` (`tasks/service.py:82` vs `:98`), and `create_agent` initializes Git and resolves its sandbox dependency volume before `start_agent_run` (`agents/service.py:92`, `:123`, `:133`) | All four writer starts establish an active row before sandbox Git or Docker work (5.4.2) |
| "A sandbox lease is enough serialization" | **Incomplete.** The canonical mirror is shared by every sandbox of a project | Project-scoped mirror lock, fixed lock order (5.5) |

### 2.1 The hardlink argument, corrected

The original defence of `git clone --local` was that Git never rewrites an
object in place, so hardlinked object files cannot be corrupted. That is true of
**Git**, and it is the wrong threat model.

The agent is not Git. It runs arbitrary shell commands, it has the workspace
mounted read-write including `.git` (`agents/service.py:147`), and the agent
images declare no `USER` directive — verified: `agent-images/*/Dockerfile`
contains no `USER`, `useradd`, or `adduser`, so the process runs as root.

A single redirect into `.git/objects/…` writes through the hardlink into the
canonical mirror and every sibling clone that shares that inode. That is exactly
the accidental-destructive-action blast radius the target architecture names as
the reason Docker isolation exists.

**Decision: use `--no-local` when cloning from the local mirror.** It forces the
regular transport and copies objects. Measure clone time and storage before
optimizing. The earlier cross-filesystem hardlink claim about Docker volumes was
also never tested, so nothing is lost by dropping it.

### 2.2 Blue-green, corrected

Postponing blue-green sync is still right. Claiming destroy-and-recreate is
equivalent was not. A sandbox can hold unpushed commits, uncommitted workspace
changes, and database state that no migration reproduces.

**Decision: postpone blue-green, but keep its seam.** Sync creates a controller
safety ref before touching anything and requires a clean workspace. That makes
the operation recoverable without building the parallel-environment machinery.

### 2.3 There is no migration runner

`ControllerStore.initialize` (`controller/store.py:468`) runs one
`INITIAL_MIGRATION` script through `executescript`, then
`INSERT OR IGNORE … VALUES (1, …)`. Every statement is
`CREATE TABLE IF NOT EXISTS`.

Adding columns to the `sandboxes` DDL therefore **does nothing to an existing
database**. The table already exists, the statement is skipped, and the new
columns never appear. This is the single most consequential finding in either
review: it moves an ordered migration runner to step 1 and invalidates the claim
that the early steps are behaviour-preserving.

---

## 3. Current architecture

### 3.1 Sandbox creation

`projects/router.py:73 create_project_sandbox` → `projects/service.py:169
register_project`.

1. Validates the host path under `PROJECTS_ROOT`.
2. `project_id = uuid5(NAMESPACE_URL, "orchestrator-project:{source_path}")`. Deterministic.
3. `sandbox_id = uuid4().hex`. **Random.**
4. Under `_sandbox_creation_lock`, a process-local `Lock`, scans every volume
   labelled `orchestrator.project.managed=true` and picks the next `-sandbox-N`
   name (`:550 _next_sandbox_name`).
5. Creates `orchestrator-project-<slug>-<sha256(source_path + name)[:10]>`
   (`:583`) plus `<volume>-controller-metadata`.
6. Starts a detached `alpine` container running
   `tar -C /source … | tar -C /project` (`:93 COPY_COMMAND`).
7. The router then writes the SQLite row (`router.py:83`).

Docker volume labels are the discovery mechanism: `list_registered_projects`,
`inspect_registered_project`, and `inspect_project_copy_job` all read labels.
`controller/lifecycle.py:168` back-fills rows from labels as `discovered`.

`.git` is not in `EXCLUDED_DIRECTORY_NAMES` (`:61`), so the copy duplicates the
whole object database, the reflog, and `.git/config` including its remotes.

### 3.2 Git

| Anchor | What it does |
| --- | --- |
| `projects/service.py:906 ensure_git_baseline` | `git init -b main` if `.git` is not a directory, sets identity, appends `.agent/ .claude/ .orchestrator/` to `.git/info/exclude`, commits `sandbox baseline` if there is no HEAD |
| `tasks/service.py:56 start_task` | Cuts `task/<uuid4hex>` from current HEAD, records `base_branch`, snapshots pre-existing dirty state |
| `tasks/service.py:791 _accept_script` | Refuses first, mutates last, then `merge --ff-only` and `branch -d` |
| `tasks/service.py:871 _reject_script` | Returns to base branch, `branch -D` |
| `tasks/service.py:903 _run_git` | Hardened throwaway container. Duplicated at `delegation/delivery.py:669` and inline at `projects/service.py:920` |
| `delegation/delivery.py:59 capture_feature_target` | Pins `(base_branch, base_commit, head_commit)`, refuses if branch or HEAD moved |
| `delegation/delivery.py:240 merge_feature_to_source` | Mounts host source `rw`, sandbox `ro`, `git fetch` then `merge --ff-only`, hooks bypassed |

No GitHub integration exists. Grep finds `github` only in
`implementation_context/inventory.py:39`, reading workflow files as CI evidence.

`sandboxes.baseline_commit` is set once and is "the first commit ever made, not
the current one" (`tasks/service.py:79`).

**Linked worktrees and submodules break the baseline script — but they fail
closed.** `GIT_BASELINE_SCRIPT` tests `if [ ! -d .git ]`. In a linked worktree or
a submodule, `.git` is a *file* holding a `gitdir:` pointer to a host path that
does not exist inside the container, so the test passes and `git init -b main`
runs.

Measured, not assumed. With the `gitdir:` target absent, Git 2.50.1 returns:

```text
fatal: not a git repository: …/main-repo/.git/worktrees/linked
exit=128
```

The `.git` file is preserved and the script's `set -eu` halts baseline creation.
An earlier draft of this document claimed history is silently replaced by a
synthetic baseline; that was wrong, and the distinction matters — this is a
compatibility defect that refuses the sandbox with an unhelpful error, not data
loss.

It remains a live defect worth fixing: the failure should name the linked
worktree as the cause rather than surfacing a raw Git fatal.

### 3.3 Database

Lives in `previews/service.py` and is bound to a **preview run**, not to a
sandbox. MySQL protocol only. Modes: `isolated`, `shared_server`, `shared_data`.
Migrations run at preview start (`:1244`), skipped for guests. Default
persistence is ephemeral. No templates, no schema hash, no `reset-db`.

Full anchors in section 8.1.

### 3.4 Runtime, network, caches

| Anchor | What it does |
| --- | --- |
| `previews/service.py:3448 _network` | Per-preview network `orchestrator-preview-<run_id[:12]>` |
| `previews/service.py:3471 _gateway_proxy` | Gateway binds the approved service to `127.0.0.1` |
| `previews/service.py:3550 _dependency_volume_name` | `orchestrator-deps-<sandbox_id[:12]>-<lockfile_digest[:12]>` |
| `previews/service.py:1422 _export_commit` | `git archive` of one commit into a fresh volume |

Dependency volumes are `ro` to the agent and `rw` to preview containers (see
2). Playwright and Chromium are pinned into the agent images (ADR 0008).

`create_preview_run` is called at `previews/service.py:544`, **after** containers
and networks exist. A crash between the two leaves resources with no run record —
the same inversion as sandbox creation.

### 3.5 Agents and turns

`agents/service.py:71 create_agent` creates an **optional** container: workspace
`rw`, credentials `rw`, dependencies `ro`, read-only root, `cap_drop=ALL`,
`pids_limit=512`, `auto_remove=True`. It sets no network option, so it runs on
the default bridge with outbound access, as root.

`tasks/runner.py:102 run_coding_turn` runs short-lived containers with the same
hardening. `jobs.py` dispatches long turns onto daemon threads.

### 3.6 Lifecycle assumptions

- **A sandbox is a one-time snapshot.** No sync operation exists.
- **One active row per subsystem, and no cross-class exclusion.** Partial unique
  indexes in `controller/store.py` bound each class separately:
  `one_active_agent_per_sandbox` (`:51`), `one_open_task_per_sandbox` (`:178`),
  `one_active_delegation_per_sandbox` (`:296`),
  `one_running_run_per_delegation` (`:414`). Nothing constrains them jointly, and
  the nesting is deliberate — a delegation holds a task, and a task preview holds
  a task. See C13 and 5.4.2.
- Reconciliation runs once at startup (`controller/lifecycle.py:114`). Only
  preview expiry loops. It audits and marks; it never recreates.
- The backend must run as one worker. Creation locks are process-local.
- `remove_project` (`projects/service.py:312`) takes a project *name*, sweeps by
  label, and deletes the row. A repeated destroy returns 404 rather than
  "already destroyed".

---

## 4. Gap analysis

### 4.1 Already aligned

| Target | Where it exists |
| --- | --- |
| Many sandboxes per project | `-sandbox-N`, `project_id` grouping, shared server per project |
| Docker as isolation layer | Throughout |
| Persistent workspace volume | Project volume |
| Shared immutable infrastructure | Dependency volumes keyed by lockfile digest; Playwright in the image |
| Idempotent operations | `_accept_script`, `_shared_volume`, `_create_shared_server` |
| Blast radius, not adversarial boundary | ADR 0006 |
| Controller-owned privileged actions | Source merge is controller-only; DB root never reaches an app container |
| Trusted metadata in SQLite | ADR 0001 |
| One active row per subsystem | The four partial unique indexes above. Cross-class exclusion is missing — see C13 |

### 4.2 Conflicts

| # | Conflict |
| --- | --- |
| C1 | Resource names derive from project name, source path, or random run IDs — not from the sandbox ID |
| C2 | Intent is written after resource creation, in both the sandbox and preview paths. The preview case also blocks atomic writer admission (5.4.2) |
| C3 | Copy, not clone. Full object-database duplication per sandbox |
| C4 | No canonical repository and no `origin/main` |
| C5 | No pinned base commit. `baseline_commit` is the first commit, possibly synthetic |
| C6 | No sync, no staleness |
| C7 | Database lifetime is preview-scoped, not sandbox-scoped. No `reset-db` |
| C8 | No push, no PR, no GitHub credentials |
| C9 | Git remotes survive the copy. ADR 0005 claims they are stripped; no code does it |
| C10 | Cleanup is name-driven, not manifest-driven. No tombstone |
| C11 | No ordered SQLite migrations (2.3) |
| C12 | Linked worktrees and submodules fail baseline creation with a raw Git fatal (3.2) |
| C13 | No cross-class writer exclusion. Four per-table indexes cannot express it, and `delegation/execution.py:163` starts a task unguarded (5.4.2) |
| C14 | Project identity is the host path, and `source_path` is `NOT NULL UNIQUE` (5.3) |

### 4.3 Reusable

The hardened git executor; `ControllerStore` transaction and event patterns; the
label taxonomy; dependency-volume keying as a template-key precedent;
`delivery.py`'s refuse-first structure; `jobs.submit_docker_job`;
`reconcile_controller_state` as the orphan-sweep host; `propose_preview`'s
propose-then-confirm pattern; protected-file and secret handling; the preview
gateway and isolated-network model.

### 4.4 Replace or retire, for new sandboxes only

Host-folder `tar` copying; random sandbox IDs; numbered names as primary
identity; lazy baseline creation; arbitrary copied base branches;
`_next_sandbox_name` and `_sandbox_creation_lock`; the copy-job metadata
apparatus (`_read_persisted_copy_status`, `_copy_job_from_persisted`, the
`STATUS_STORAGE_PROJECT_VOLUME` branch); local host-folder merge as the primary
publish path; `shared_data` for new sandboxes.

### 4.5 Documentation that conflicts with code

ADR 0005's remote-and-hook-stripping claim, and ADR 0003's read-only dependency
claim. Both are load-bearing for safety arguments the code does not make. Fix
the code or fix the ADRs.

---

## 5. Recommended v1

### 5.1 Ownership boundaries

| Module | Responsibility |
| --- | --- |
| `app/projects/` | Host folder browsing. Canonical repository registration and fetch |
| `app/sandboxes/manifest.py` *(new)* | Identity, intent, lifecycle state |
| `app/sandboxes/lifecycle.py` *(new)* | Create, resume, sync, destroy, converging and idempotent |
| `app/sandboxes/git.py` *(new)* | The one hardened git executor. Absorbs the three `_run_git` copies |
| `app/sandboxes/database.py` *(new)* | Engine protocol, one database per sandbox, `reset-db` |
| `app/sandboxes/publish.py` *(new)* | Push, PR discovery, PR creation |
| `app/previews/`, `app/agents/`, `app/tasks/` | Unchanged, but resolve resources through the manifest |

Plain services with deterministic helpers. No generic resource-graph or saga
framework.

### 5.2 Identity and naming

Require an immutable `feature_key` on the create request. Display titles stay
separate and mutable.

```python
sandbox_id = uuid5(NAMESPACE_URL, f"{project_id}:{feature_key}").hex   # full 128 bits
short_id   = sandbox_id[:12]                                          # Docker names only
```

Note `.hex`. A `UUID` is not subscriptable.

**The full hex is the identity.** It is what the manifest primary key, the
ownership labels, and the database name use. The 12-character `short_id` appears
only inside Docker resource names, where length matters.

Twelve hex characters is 48 bits. At one million sandboxes the birthday
collision probability is 0.18%; at ten thousand it is 1.8e-5%, and at one
thousand it is 1.8e-7%. This is a single-worker local tool, so the realistic
figure is negligible — but carrying the full hex in labels and the database
costs nothing and removes the question. Resource lookup still validates
ownership labels, so a truncated name is never the only proof of identity.

```text
sbx-<sandbox_id>-ws        workspace volume
sbx-<sandbox_id>-agent     primary agent container
sbx-<sandbox_id>-net       sandbox network
sbx_<sandbox_id>           database
sbx-<sandbox_id>-preview-* preview resources
```

A content-derived ID makes `create` converge: a repeated request finds the same
manifest and the same resources instead of creating a sibling.

**Keep the per-sandbox network in v1.** The target names isolated runtime and
network configuration as a per-sandbox property, and one `docker network create`
is cheap. An earlier draft defined the network and then cut it; that was an
internal contradiction.

Preview teardown does not endanger it, but the ownership rule must be explicit.
`_preview_networks` (`previews/service.py:3668`) filters on **both**
`LABEL_MANAGED=true` and `LABEL_RUN_ID=<run_id>`, so a network labelled to the
sandbox is never discovered by a preview's cleanup.
`_disconnect_foreign_endpoints` (`:3699`) then runs only against networks the
run owns. The shared database server already relies on exactly this: it outlives
every run that connects to it.

The rule to state and test: **a sandbox-owned network carries the sandbox label
and no run label; preview teardown removes only run-labelled networks and
disconnects rather than removes anything it does not own.** A preview that needs
to reach the sandbox database joins the sandbox network as a borrowed endpoint
and is disconnected, not removed, at teardown.

### 5.3 Manifest

Extend `sandboxes` — via a real migration (2.3) — rather than adding a second
table.

| Table | Group | Fields |
| --- | --- | --- |
| `projects` | Canonical repo | `remote_url`, `default_branch`, `mirror_volume`, `source_path` (legacy import metadata) |
| `sandboxes` | Version | `lifecycle_version` (`legacy` or `v1`) |
| `sandboxes` | Identity | `feature_key`, `feature_title` |
| `sandboxes` | Lifecycle | `desired_state`, `lifecycle_status`, `operation`, `operation_phase`, `last_error` |
| `sandboxes` | Git | `base_ref`, `created_base_commit`, `current_base_commit`, `pending_base_commit`, `feature_branch` |
| `sandboxes` | Runtime | `agent_provider`, `network_policy` |
| `sandboxes` | Database | `db_engine`, `db_name`, `schema_baseline_hash`, `db_data_volume` (SQLite only) |
| detection record | Engine | `signals_json`, `proposed_engine`, `confirmed_engine`, `migrate_commands_json`, `seed_commands_json`, `actor`, `confirmed_at` |
| `sandboxes` | Publish intent | `publish_remote`, `remote_branch`, `pr_requested` |

One `intent` field cannot express both what is wanted and where a failed
operation stopped. Five lifecycle fields can, and that is enough — a generic
workflow engine is not needed.

`lifecycle_status` takes exactly these values:

| Value | Meaning | Leaves via |
| --- | --- | --- |
| `creating` | Provisioning resources | Completion, or `awaiting_engine_confirmation` |
| `awaiting_engine_confirmation` | Waiting on a human. **Lease released** | `confirm-engine` |
| `ready` | Usable. The only state that admits writers | Any lifecycle operation |
| `syncing` | Git moved, migrations running. `pending_base_commit` set | `ready`, or `database_failed` |
| `database_failed` | Migrations failed. Not reversible | A finalizing `reset-db` |
| `draining` | Destroy declared; writers stopping, no new writers admitted | `destroying` |
| `destroying` | Lease held, resources being removed | Tombstone |
| `degraded` | Resume found an inconsistency it will not repair silently | Human decision |

`desired_state` is only `active` or `destroyed`. It records what should be true;
`lifecycle_status` records what is true now.

**Lifecycle state must not reuse `sandboxes.status`.** `register_sandbox`
(`controller/store.py:528`) has an unconditional
`ON CONFLICT(id) DO UPDATE SET … status = excluded.status`, and five call sites
pass a status: `tasks/service.py:74` and `projects/service.py:306` pass `ready`,
`previews/service.py:3843` passes `ready`, `projects/router.py:89` passes the
copy job status, and `controller/lifecycle.py:183` passes `discovered`.

Starting a task or a preview would therefore silently overwrite `destroying`,
`database_failed`, or a tombstone with `ready`. The existing `status` column
keeps its current meaning — import and discovery state — and `register_sandbox`
keeps writing it. Lifecycle lives in the new `lifecycle_status` column, which
`register_sandbox` never touches. This also keeps every existing call site
working unchanged.

**Canonical repository fields belong to the project, not the sandbox.** The
mirror is shared by every sandbox of a project. Today `projects` holds only
`id`, `source_path`, and `created_at` (`controller/store.py:21`), and
`project_id` is `uuid5` of the host path (`projects/service.py:960`) — so two
checkouts of one remote become two unrelated projects.

**Additive columns alone will not achieve this.** Two existing semantics block
it: `source_path` is `NOT NULL UNIQUE`, and `register_sandbox`
(`controller/store.py:520`) upserts with
`ON CONFLICT(source_path) DO UPDATE SET id = excluded.id`, so the host path is
both the natural key and the identity source. Adding `remote_url` next to them
changes nothing on its own.

The migration therefore has to define four things:

1. **Normalization.** `git@github.com:o/r.git`, `https://github.com/o/r.git`,
   and `https://github.com/o/r` are one project. Lowercase the host, strip a
   trailing `.git`, drop the SCP-versus-URL difference.
2. **Credential stripping.** A remote may embed a token or username. Never store
   userinfo; strip it before hashing and before persisting.
3. **Uniqueness and nullability.** `remote_url` becomes the unique key when
   present. `source_path` must become nullable, since a v1 sandbox created from a
   remote has no host folder.
4. **The legacy-to-v1 store path.** `register_sandbox` keeps its `source_path`
   upsert for legacy imports. V1 creation uses a separate path keyed on the
   normalized remote. The two must not share one upsert, or a legacy import will
   reassign a v1 project's ID.

For v1, `project_id = uuid5(NAMESPACE_URL, f"repo:{normalized_remote}")` when a
remote exists, and the current path-derived ID otherwise.

**Two baseline commits, not one.** `created_base_commit` is immutable and is the
audit anchor. `current_base_commit` moves only after a successful sync.
Staleness is measured against `current_base_commit`; measuring against the
creation pin leaves every synced sandbox permanently stale.

Observed results go in a separate publish record: remote branch SHA, PR number,
PR URL, PR state, last pushed commit, last error.

Do **not** store container IDs, container status, network IDs, host ports, or
disk usage. Docker answers those, and a copy will drift.

### 5.4 Lifecycle operations

All six primary operations plus the `confirm-engine` transition are v1.
Sequencing them late in the plan is fine; moving them out of v1 is not.

| Operation | Steps |
| --- | --- |
| **create** | Derive ID → insert manifest `desired_state=active, lifecycle_status=creating` → controller fetches `origin` into the project mirror → pin `created_base_commit` and `current_base_commit` from the mirror's `default_branch` → create workspace → clone from the mirror with `--no-local` → create feature branch at the pin → detect engine → `awaiting_engine_confirmation` if unconfirmed → provision database → create network and agent container → `lifecycle_status=ready` |
| **resume** | Require `desired_state=active` → verify workspace volume, repository identity, expected feature branch, database, network → recreate safe missing resources → preserve worktree and branch → report `degraded` when an inconsistency is unsafe to repair |
| **sync** | Refuse if any active writer exists (5.4.2) → create Git safety ref → controller fetches `origin` into the mirror → sandbox fetches the mirror → rebase before a PR, merge after → replay migrations → update `current_base_commit` only on success → on Git failure restore from the safety ref; on migration failure set `lifecycle_status=database_failed` (see 5.4.1) |
| **reset-db** | Mark running → refuse if any active writer exists (5.4.2) → terminate connections → drop → recreate role and database → migrate and seed from the current feature commit → record `schema_baseline_hash` |
| **publish** | Verify the reviewed head commit → push the remote branch → search for an existing PR by head branch → create only if absent → record the result |
| **destroy** | `desired_state=destroyed, lifecycle_status=draining` and stop admitting writers → stop existing writers → acquire the lease → `lifecycle_status=destroying` → remove containers and networks → drop database and role → remove workspace and sandbox-owned volumes → preserve shared infrastructure → keep a tombstone |

#### 5.4.1 Sync is not fully reversible in v1

An earlier draft claimed sync aborts and restores on failure. A Git safety ref
restores **Git only**. It cannot undo applied migrations or seed commands.

For v1, state the limit rather than implying a guarantee that does not exist:

- Git failure during rebase or merge → restore from the safety ref. Reversible.
- Migration failure after the Git step succeeded → `lifecycle_status=database_failed`.
  **Not reversible.** The recovery path is `reset-db`, which rebuilds the
  database from migrations and fixtures at the current commit.

**The failed state needs its own metadata, or recovery cannot complete.** Sync
moves Git first and updates `current_base_commit` only on full success. After a
migration failure the workspace holds the new base while `current_base_commit`
still names the old one. `reset-db` rebuilds the database but knows nothing
about that discrepancy, so a "successful" reset would leave the manifest
permanently lying about the baseline — and staleness would be computed from the
wrong commit.

Persist the operation target. Sync writes `pending_base_commit` before touching
Git and clears it only on completion:

| Point | `current_base_commit` | `pending_base_commit` | `lifecycle_status` |
| --- | --- | --- | --- |
| Before sync | old | null | `ready` |
| Git done, migrations running | old | new | `syncing` |
| Migration failed | old | new | `database_failed` |
| After recovering `reset-db` | **new** | null | `ready` |

`reset-db` therefore has two jobs when it runs against `database_failed`: rebuild
the database, then finalize `pending_base_commit` into `current_base_commit` and
return the sandbox to `ready`. Run against a healthy sandbox, it finds no pending
commit and only rebuilds. Same operation, one extra conditional.

This is acceptable precisely because sandbox database state outside migrations
and fixtures is defined as ephemeral. It is the reason `reset-db` is a v1
operation rather than a convenience. Database snapshots and blue-green swaps
would make sync reversible; both are postponed.

#### 5.4.2 Lifecycle lease and writer sessions

**Confirmed: `sync` and `reset-db` refuse while a preview is active.** A preview
application holds connections to the sandbox database and can write to it during
a migration replay or between a drop and a recreate. A live preview also
bind-mounts the workspace itself, so it can observe a half-rebased tree.

**A read-only predicate is not enough.** The four partial unique indexes each
constrain a separate table. None of them, alone or together, enforces "one
writer per sandbox", and a check that merely *reads* them is a
time-of-check-to-time-of-use race: a delegation can pass the check and then
start a task. `delegation/execution.py:163` is that path — `_execute_run` calls
`start_task` with no writer check of its own.

So this must be an **admission mechanism, not a predicate**. But it must not be
a single exclusive gate that every writer claims: writers legitimately nest. A
delegation starts a child task (`delegation/execution.py:163`), and a task
preview requires an open task while it creates its preview run
(`previews/service.py:261 _preview_target`). Under one exclusive gate the child
could never claim it, and both workflows would deadlock.

The two things are asymmetric, so model them asymmetrically:

| | Lifecycle lease | Writer session |
| --- | --- | --- |
| Held by | **Every lifecycle mutation**: `create`, `resume`, `confirm-engine`, `sync`, `reset-db`, `publish`, `destroy` | agent, task, preview, delegation |
| Cardinality | At most one per sandbox, exclusive | Nests; already bounded per class |
| Excludes | Every writer session start, and other leases | Nothing directly |
| Start rule | Refuse if any writer session is active (except `destroy`, see below) | Refuse if a lifecycle lease exists |

Every operation that creates, repairs, or removes Git, database, network, or
container resources takes the lease — not only the destructive four. `create`
and `resume` both provision resources, and a `resume` racing a `destroy` is
exactly the conflict the lease exists to prevent.

**Release the lease across human waits.** `create` stops at
`awaiting_engine_confirmation` (5.4.3) and may sit there indefinitely. Holding
the lease would block every other operation on that sandbox until a person
returns. `create` therefore releases on entering that state, and `confirm-engine`
claims a fresh lease when it resumes provisioning.

**Atomicity is the whole point, so each check and claim share one write
transaction.** Start these admission transactions with write intent, such as
SQLite `BEGIN IMMEDIATE`, before reading coordination state. A lifecycle start
checks all active writer rows and inserts its unique lease in that transaction.
A writer start checks that the sandbox admits writers and has no lease, then
inserts its active row in that transaction. A separate check followed by an
insert has the same time-of-check race.

All four writer starts need an explicit admission order:

- **Tasks touch sandbox Git before recording the row.** `start_task` runs
  `ensure_git_baseline` at `tasks/service.py:82`, then calls `create_task` at
  `:98`. Add a controller-only `preparing` task status and include it in
  `one_open_task_per_sandbox`. The admission transaction checks the lease and
  inserts the `preparing` row with its task ID and branch. The new row leaves
  `base_branch` and `base_commit` null during preparation. The migration makes
  `base_commit` nullable for this state. The service then reads the baseline,
  creates the branch, fills the base fields, and moves the task to `open`. A
  preparation failure moves the row to `failed`.
- **Agents touch sandbox Git and Docker before recording the row.**
  `create_agent` initializes the Git baseline at `agents/service.py:92`, resolves
  the sandbox dependency volume at `:123`, and calls `start_agent_run` at `:133`.
  Reconcile any stale agent first. The admission transaction then checks the
  lease and inserts the existing active `created` row. Only then may the service
  touch sandbox Git, the dependency volume, or the agent container. A failure
  moves the row to `failed` and cleans up resources created by that attempt.
- **Previews start Docker resources before recording the row.** `_start_native`
  runs at `previews/service.py:443`; `create_preview_run` inserts at `:544`. The
  admission transaction checks the lease and inserts a `preparing` row before
  Docker creation. A preparation failure moves the row to `failed`. This also
  fixes C2.
- **A delegation becomes active when its revision is created.**
  `create_delegation_revision` does this at `delegation/service.py:107`.
  `one_active_delegation_per_sandbox` covers `ready`, `running`, and `halted`.
  The lease check and ready-row insert therefore share the revision-creation
  transaction.

**Destroy drains rather than refuses.** It cannot use the ordinary start rule,
because it is the one operation whose purpose is to end active writers:

1. In one admission transaction, set `desired_state=destroyed` and insert the
   destroy lease. Destroy alone can claim the lease while writers exist.
2. Stop the writers that already exist while the lease blocks new work.
3. Remove resources, write the tombstone, and release the lease.

Step 1 is what makes step 2 terminate. Without it a delegation could start a new
task while destroy is stopping the previous one. Other lifecycle starts also
refuse while the destroy lease exists.

**A persisted lease needs crash recovery.** The lease outlives the process that
took it, so a killed backend leaves a lease no one holds and the sandbox becomes
permanently unusable. Record the owning operation ID and a timestamp, and have
`reconcile_controller_state` reclaim leases whose operation is already settled —
it does exactly this for interrupted turns today
(`controller/lifecycle.py:30 _settle_interrupted_turns`). Resume may reclaim a
lease for its own sandbox on the same evidence.

This keeps nesting working unchanged. It adds one check to each writer start,
and gives lifecycle operations the exclusion they need. Parent-child ownership
tokens would also work, but they require every writer to know its parent — a
much larger change for the same guarantee.

Four writer classes, not three. Delegations were missing from the earlier draft:

| Writer | Detection | Backing constraint |
| --- | --- | --- |
| Coding agent | `active_agent(sandbox_id)` | `one_active_agent_per_sandbox` (`store.py:51`) |
| Open task | `open_task(sandbox_id)` | `one_open_task_per_sandbox` (`store.py:178`) |
| Active preview | `active_preview(sandbox_id)` (`store.py:2265`) | `one_active_preview_per_sandbox` (`store.py:77`) |
| Active delegation | `delegations_for_sandbox(sandbox_id)` | `one_active_delegation_per_sandbox` (`store.py:296`) |

`active_preview` covers `preparing`, `running`, `restarting`, `rebuilding`, and
`stopping`. `stopping` belongs in the set: a stack that is still shutting down
can still hold an open connection.

The indexes stay. They remain the correct per-class constraint, and the lease
does not replace them. It adds lifecycle-versus-writer exclusion, while the
existing indexes continue to bound each writer class.

**Refuse by default; stop only on explicit request.** A running preview is
usually a human with an open browser tab, so silently killing it to service a
background sync is the wrong default. The API should return 409 naming the
blocking writer, and accept an explicit opt-in that stops the preview first.

The mechanism already exists and has a precedent: `tasks/service.py:708
_stop_task_preview` stops a stack and removes its run-scoped volumes as part of a
lifecycle transition, and deliberately leaves the lockfile-keyed dependency
volume in place. Reuse it rather than writing a second teardown path.

`destroy` differs: it stops writers rather than refusing, because the caller has
already declared the sandbox should cease to exist.

This also closes a gap in `reset-db`'s "terminate connections" step. Terminating
connections without first removing the writer only invites a connection pool to
reconnect mid-drop.

#### 5.4.3 Engine confirmation is a lifecycle state

Detection proposes; a human confirms (8.3). Creation therefore cannot run
straight through to `ready` when detection is unconfirmed or ambiguous.

Add `awaiting_engine_confirmation` as a `lifecycle_status`, and a persisted
detection record holding the signals found, the proposed engine, the confirmed
engine, the actor, and the timestamp. Without it a retry cannot distinguish an
unconfirmed proposal from confirmed intent, and would re-provision against a
guess.

**A state needs a transition out of it.** `confirm-engine` is a first-class
lifecycle operation, not an implicit side effect: it takes the sandbox ID, the
chosen engine, and the actor; it writes the confirmed engine and the migration
configuration into the detection record; and it resumes creation from the
database-provisioning step. Without it, `awaiting_engine_confirmation` is a
terminal state and creation can never finish.

**Migration and seed configuration must be controller-owned and explicit.**
`reset-db` runs migrations and seeds for three engines, but
`detection.py:506 _native_dependencies` knows only `npx prisma migrate deploy`
and `npm run db:seed:preview`. Nothing supplies those commands for a Django,
Rails, or Alembic project, and they must not be read from agent-editable preview
files — that would let a coding agent choose what the controller executes.

So the detection record carries the commands, and `confirm-engine` is where a
human supplies or approves them. Engine detection proposes a default set when it
recognizes the stack; otherwise the field is empty and confirmation requires it.
This is the same propose-then-confirm shape as the engine itself, and it is a
prerequisite for step 6 rather than a detail inside it.

A project whose engine and commands are already confirmed skips this state
entirely.

Every step uses get-or-validate. **A retry must reject a resource that has the
expected name but the wrong ownership labels** — a deterministic name alone is
not proof of ownership.

A repeated destroy returns the destroyed outcome, not 404.

The persisted lifecycle lease is the one authoritative per-sandbox coordination
primitive. Its unique sandbox row excludes other lifecycle mutations. The
writer-start transactions exclude agents, tasks, previews, and delegations
while the lease is held. Do not add a second process-local lifecycle lock. The
project mirror lock has a different scope and uses the fixed order in 5.5.

### 5.5 Git lifecycle

The canonical mirror is a controller-owned bare repository per project with one
configured origin, fetched on demand at create, at sync, and at every staleness
inspection. It is never mounted read-write into an agent and never depends on the
user's host checkout.

**The mirror needs its own project-scoped lock.** The sandbox lease serializes
operations *within* one sandbox, but the mirror is shared by every sandbox of a
project — and create, sync, and staleness inspection all fetch into it. Two
sandboxes of one project can therefore fetch concurrently and contend on Git's
own lock files, producing a spurious failure in an operation that did nothing
wrong.

One lock per project covers mirror creation, origin validation, and fetch. It is
held only for the fetch itself, never for the clone or any sandbox work, so it
does not serialize sandbox creation beyond the seconds a fetch takes. Two locks
with clearly different scopes is simpler than one lock trying to cover both, and
the lock order is fixed — sandbox lease first, then mirror lock — so the pair
cannot deadlock.

Each sandbox gets a full independent clone: its own `.git`, refs, config, index,
hooks, and one feature branch. Clone with `--no-local` (2.1).

**Fetch is two steps, and the sandbox never talks to GitHub.** An earlier draft
said the clone strips its remotes and then said sync fetches `origin/main` — a
contradiction, because the stripped clone has no `origin`.

1. **Canonical fetch.** The controller fetches `origin` into the project mirror
   volume. This is the only step that touches GitHub, and the only step that
   needs network access or credentials.
2. **Sandbox import.** The sandbox fetches from the mirror, which is a local
   volume mounted into the git container. No remote, no network, no credentials.

The sandbox clone keeps exactly one remote pointing at the mirror, or none at
all with the mirror passed as a path per command. Either is fine; what matters
is that no GitHub URL and no credential ever reaches the sandbox. Strip inherited
remotes and hooks in the clone step. That closes C9 and makes ADR 0005 true.

**Private repositories need controller-owned read credentials at create and
sync**, not only at publish. Section 5.6 covers the split. These are read
credentials used by the canonical fetch; the publish helper's write credentials
stay separate.

Task branches stay. They branch from the sandbox feature branch and fast-forward
back into it.

Staleness is `git rev-list --count <current_base_commit>..<base_ref>`, computed
on request against the mirror. Store nothing. Staleness is informational and
never triggers action.

**A staleness check fetches first, and says so.** Reading the mirror without
fetching reports staleness since the last fetch, which is a different and
misleading number — a sandbox can look current simply because nobody has synced
the project recently. The inspection endpoint therefore performs a canonical
fetch (which is why it needs read credentials, 5.6) and returns the mirror's
fetch timestamp alongside the count, so a caller can tell a fresh answer from a
cached one. Fetch failure degrades to the last known state, labelled as such,
rather than reporting zero.

V1 may require a remote and a default branch for new managed sandboxes. Folders
without a remote stay on the legacy path.

### 5.6 Environment and credentials

Secrets stay outside the workspace. Generated database credentials are injected
into runtime containers and are never written to `.env` files in the workspace.

Provider credential volumes remain a deliberate shared, mutable exception.
**GitHub credentials must never enter them.**

GitHub access splits in two, and neither half reaches a sandbox or agent
container:

| Credential | Used by | When |
| --- | --- | --- |
| Read | Canonical fetch into the project mirror | Create, sync, staleness |
| Write | Publish helper | Push and PR creation only |

A single token may back both, but the code paths stay separate so a read-only
deployment is possible and so publish remains the only step that can mutate the
remote.

---

## 6. Refactor plan, in dependency order

| Step | Work | Notes |
| --- | --- | --- |
| 1 | **Ordered SQLite migration runner.** Then additive `sandboxes` columns, including `lifecycle_status` as a column distinct from `status`; and a **table rebuild** for `projects` | Hard prerequisite. Nothing else is safe without it |
| 2 | Manifest fields, deterministic identity (full hex), one shared resource-name module, sandbox-ID routes alongside legacy name routes | Requires the `feature_key` decision in 11.2 |
| 3 | Collapse the three `_run_git` copies into `app/sandboxes/git.py` | Behaviour-preserving. Covered by `tests/test_git_baseline.py` and the task tests |
| 4 | Project-owned canonical mirror, two-step fetch, `--no-local` clone, strip remotes and hooks, pin both baseline commits, controller read credentials, reject linked-worktree sources on the legacy import | Where the design changes |
| 5 | **5a.** Establish every writer row before sandbox work. Add task `preparing` and make `base_commit` nullable only during preparation. Move `start_agent_run` before sandbox Git and dependency work. Move `create_preview_run(status=preparing)` before Docker creation, which also fixes C2. Check the lease during `create_delegation_revision`. **5b.** Add the per-sandbox lifecycle lease with crash reclaim and a no-lease check inside every active-row transaction. **5c.** Add the project-scoped mirror lock. **5d.** Add converging `create`, `resume`, and `destroy` with draining, phase checkpoints, label-ownership validation, and tombstones | The active-row reorder is a prerequisite for the lease. Task preparation requires an ordered migration for its status, partial index, and nullable `base_commit` |
| 6 | One database per sandbox, engine detection with `awaiting_engine_confirmation` and a `confirm-engine` transition, controller-owned migration commands, `reset-db`, SQLite data volume; previews consume the sandbox database; disable `shared_data` for new sandboxes | See section 8 |
| 7 | Staleness inspection with fetch, then sync: safety refs, `pending_base_commit`, pre-PR rebase, post-PR merge, `database_failed` on migration failure | Depends on 6 — `reset-db` is the recovery path and finalizes the pending commit |
| 8 | Push, PR discovery, PR creation, partial-failure recovery | |
| 9 | Manifest-driven destroy sweep and startup orphan reporting for `sbx-*` | |

Only step 3 is genuinely behaviour-preserving. Step 1 changes persistence
mechanics, and step 2 changes what a row means.

**Step 1 is not purely additive.** `sandboxes` takes new columns and nothing
else. `projects` cannot: 5.3 requires `source_path` to become nullable and
`remote_url` to become the unique key, and SQLite's `ALTER TABLE` cannot drop a
`NOT NULL UNIQUE` constraint. That means the standard twelve-step rebuild —
create the new table, copy rows, drop the old, rename, recreate indexes, all
inside one transaction with `PRAGMA foreign_keys=OFF`. `sandboxes.project_id`
carries a foreign key to `projects(id)`, so the rebuild has to preserve those
IDs exactly. This is the riskiest single migration in the plan and deserves its
own test against a copy of a real controller database.

Step 7 now depends on step 6, not merely follows it. Sync can leave a sandbox in
`database_failed`, and `reset-db` is the only way out. Shipping sync before
`reset-db` would create a state with no recovery.

### 6.1 Backward compatibility

Existing sandboxes cannot be converted reliably. They may hold a dirty source
snapshot, an arbitrary base branch, copied remotes and hooks, synthetic baseline
history, persistent MySQL data, and task branches under current settlement
assumptions.

Mark them `lifecycle_version=legacy`. Allow inspection, task settlement, local
delivery, and destruction. Refuse `sync` and `reset-db`. Provide an explicit
recreate operation. Never silently reinterpret a legacy baseline.

The frontend should migrate from `project_name` routes to `sandbox_id` routes,
with temporary compatibility routes resolving display names.

`backend/.controller-data/` holds manual database backups. Take one before step 1.

---

## 7. Risk review

**Git isolation.** The copy preserves remotes, hooks, dirty state, and
worktree pointers. An independent clone removes most of this. Do not expose
GitHub credentials to agents. Avoid shared writable objects (2.1). Serialize
task creation, sync, and publish per sandbox, and exclude active writers from
the destructive operations (5.4.2).

**Volume persistence.** The workspace volume is the durable code state. Resume
must recreate containers without replacing it. Destroy must validate labels, not
just names. Keep tombstones for repeated deletion and orphan checks.

**Database migrations.** Bootstrap commands must come from controller-owned
project configuration, not from agent-editable preview files. A reset must
terminate connections before dropping. A migration failure leaves
`lifecycle_status=database_failed`, never `ready`. A migration failure during
sync is not reversible by the Git safety ref; `reset-db` is the recovery path
(5.4.1).

**Stale baselines.** Keep both baseline commits. Never overwrite the creation
pin. Staleness stays informational until an explicit sync.

**Crash and partial creation.** Both the sandbox and preview paths write Docker
resources before their SQLite record. Reverse both. Ordinary Docker exceptions
already roll back the sandbox path; a process crash does not, which is what
manifest-first fixes.

**Publish and PR failures.** Remote Git and GitHub are separate systems; treat
them as separate phases. The remote branch and PR head branch are the
idempotency anchors. Use `--force-with-lease` only for an intentional pre-PR
rebase, never after a PR exists. Do not mark publish complete until both the
remote commit and the PR are verified.

**Shared caches.** Image-owned Playwright already matches the target. Do not
share writable `node_modules` volumes between sandboxes — they are `rw` to
preview containers today. A package *download* cache is safer than shared
installed dependencies. Any installed-dependency cache key needs the lockfile
digest, runtime image digest, CPU platform, package-manager version, and
relevant package-manager configuration.

Dependency volumes are never removed and accumulate one per distinct lockfile
per sandbox. Current count unmeasured. Count them before adding database
templates.

**Shared database server.** One failure domain. Use separate roles, connection
limits, and per-database ownership.

---

## 8. Database engine

Detect the engine per project. Build `reset-db` as drop, recreate, migrate, and
seed, which is correct for every engine. Treat template cloning as a later
per-engine optimization, added only after provisioning cost is measured.

### 8.1 Current coupling

| Anchor | What it does |
| --- | --- |
| `previews/models.py:53 PreviewServiceType` | A `StrEnum` with exactly one member, `MYSQL` |
| `previews/models.py:78 PreviewDependencyService` | Carries `type`, `image`, `database`, `persistence`, `sharing`, `share_target` |
| `previews/detection.py:539 _mysql_prisma_schema` | The only engine detector. Matches a Prisma `mysql` datasource and nothing else |
| `previews/detection.py:517 _native_dependencies` | Hard-codes `npx prisma migrate deploy`, plus `npm run db:seed:preview` when the script exists |
| `previews/service.py:1719 _native_service_environment` | Builds `mysql://…@database:3306/…` and refuses any other service type |
| `previews/service.py:1290` | `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_ROOT_PASSWORD`, `/var/lib/mysql`, `/var/run/mysqld` |
| `previews/service.py:1988 _run_shared_sql` | Administrative SQL through the `mysql` CLI as root |
| `previews/service.py:1766 _shared_database_names` | `orchestrator-shared-db-<project_key[:12]>` plus `-data`, `-credentials`, `-net` |
| `previews/service.py:1776 _shared_schema_name` | `sbx_<sandbox_id[:16]>` |
| `previews/service.py:1785 _identifier` | "Reduces a sandbox id to characters MySQL accepts unquoted" |
| `previews/service.py:2540 _wait_for_mysql_health` | `mysqladmin ping` health wait |
| `controller/store.py:127 shared_database_schemas` | One row per sandbox holding credentials on the shared server |

Measured coupling: **37 lines** match `mysql` case-insensitively across three
files — 31 in `previews/service.py`, 5 in `previews/detection.py`, 1 in
`previews/models.py`. `previews/config.py` has none. (An earlier draft said 28;
that figure was wrong.)

The constraint is the MySQL *protocol*, not a pinned image.
`PreviewDependencyService.image` is configurable, so a project can already
supply `mariadb:11` in place of the proposed `mysql:8.4`. It cannot supply
Postgres, because the surrounding code speaks the MySQL wire protocol, uses the
`mysql` client, and sets `MYSQL_*` environment variables.

The abstraction seam exists. It has never had a second implementation.

### 8.2 What differs between engines

| Engine | Server | Template clone | `reset-db` |
| --- | --- | --- | --- |
| PostgreSQL | Shared container | `CREATE DATABASE x TEMPLATE y`, a file copy | Drop, then clone |
| MySQL | Shared container | None. Requires dump and restore | Drop schema, re-run migrations |
| SQLite | No server | Copy the template file | Delete the file, copy again |

SQLite removes most of the server machinery: no server container, no network, no
credentials, no root password, no schema name, and no grant to revoke. Most of
the shared-database code does not execute for a SQLite project.

It does **not** get isolation for free by sitting in the workspace. The database
lives in a sandbox-owned data volume mounted outside the repository tree, and
every runtime reaches it through an injected URL — see 8.6 for why the obvious
in-workspace placement is unworkable.

### 8.3 Detect, propose, confirm

Follow the house pattern. `propose_preview` never runs a detection result
automatically; it proposes and waits for a human. Engine detection does the same.

Detection runs at sandbox creation, against the canonical Git clone. The
confirmed engine is recorded in the manifest as intent and can be overridden.

Signals, in precedence order. Explicit configuration beats dependency presence.

1. Prisma `datasource db { provider = … }`. Generalize
   `detection.py:539 _mysql_prisma_schema` to return the provider instead of
   matching only `mysql`.
2. `DATABASE_URL` scheme in `.env` or `.env.example`: `mysql://`, `postgres://`,
   `postgresql://`, `file:`.
3. Django `DATABASES.ENGINE`, Rails `config/database.yml`, Alembic
   `sqlalchemy.url`.
4. `docker-compose.yml` service images: `postgres:*`, `mysql:*`, `mariadb:*`.
5. Package dependencies, as the weakest signal: `pg`, `mysql2`,
   `better-sqlite3`, `psycopg`, `asyncpg`, `PyMySQL`.

On conflicting signals, do not guess. Surface every signal and ask. Monorepos and
projects mid-migration produce conflicts, and a wrong silent guess costs a whole
sandbox.

Re-detect on sync. Report a mismatch. Never switch a sandbox's engine silently.

### 8.4 Engine protocol

Roughly five operations: `provision`, `connection_url`, `run_migrations`,
`drop`, and the `supports_template` capability flag.

Extract the existing MySQL code behind it without changing behaviour, then add
PostgreSQL and SQLite.

### 8.5 Defect this must fix

`_shared_database_names` (`previews/service.py:1766`) keys the shared server on
the project alone. A project that changes engine collides with its own existing
server container and data volume. Add the engine to that key.

### 8.6 SQLite-specific risk

If the SQLite file sits inside the workspace volume, it is inside the sandbox
Git clone. Unless it is excluded, a coding agent will commit the database into
the feature branch and it will reach the published diff.

That is the reasoning that led to the accepted design below, in which the file
does not live in the workspace at all.

`GIT_BASELINE_SCRIPT` (`projects/service.py:133`) appends `.agent/`, `.claude/`,
and `.orchestrator/` to `.git/info/exclude`, and that is the obvious mechanism —
but **it is not sufficient on its own.** `.git/info/exclude` suppresses only
*untracked* files. A project that already tracks `prisma/dev.db` keeps tracking
it, and every reset or migration then shows up as a committed change.

**Nor can the database live in the workspace at all.** A task preview creates a
fresh volume and populates it with `git archive` (`previews/service.py:1051`
and `:1444`). `git archive` exports only tracked content at a commit, so a
database that is correctly excluded is by definition absent from the export. The
task preview would then find no database and provision an ephemeral one —
silently diverging from the sandbox the human is reviewing.

Excluding the file and placing it in the workspace are therefore mutually
exclusive requirements. The resolution is to take it out of the workspace:

1. **A sandbox-owned SQLite data volume**, `sbx-<id>-db`, mounted at a stable
   path outside the repository tree. It is not in the Git clone, so nothing can
   commit it and no exclusion rule is needed.
2. **Inject its connection URL** into live previews, task previews, agent
   containers, and verification containers alike. This makes SQLite behave like
   the server engines: the sandbox owns the data, and every runtime reaches it
   through an injected URL rather than through a path inside the workspace.
3. **Detect a tracked database path at create time and refuse**, reporting the
   path. A project that commits `prisma/dev.db` needs a human decision, not a
   silent relocation.

This also restores the property the server engines have for free — `reset-db`
affects every runtime consistently, because there is one database, not one per
preview workspace.

### 8.7 Scope cut

Do not port `shared_data` to new engines. It lets one sandbox write another's
schema, and carries real ownership complexity:
`shared_database_schemas.owner_sandbox_id`, guests that must not migrate or seed
(`previews/service.py:1237`), and revocation that must not touch the owner. It
stays MySQL-only until something needs otherwise. New engines implement
`isolated` and `shared_server` only.

### 8.8 Rejected alternative: Postgres-only

One source review recommended that new managed sandboxes use Postgres only, with
existing MySQL sandboxes on a legacy path.

Rejected. It leaves every MySQL project permanently unable to use the new
architecture, which costs more than the protocol in 8.4. Forcing Postgres onto a
MySQL application is an application change, not an infrastructure change.

The valid part is kept: do not build a large multi-engine abstraction up front.
Five operations, and templates deferred until measured.

### 8.9 Ordering within step 6

| Sub-step | Work | Depends on |
| --- | --- | --- |
| a | Engine protocol. Extract existing MySQL behind it, unchanged | — |
| b | Detection at create, proposed and confirmed, stored in the manifest | Plan steps 1–2 |
| c | `reset-db` as drop plus re-migrate, all three engines | a, b |
| d | PostgreSQL template cloning | c, plus measured `reset-db` latency |

Sub-step a is mechanical and bounded by the 37 lines counted in 8.1. Effort is
otherwise unmeasured.

---

## 9. Overengineering check

Not needed for v1: continuous reconciliation; heartbeats and reapers; autonomous
repair loops; multi-host scheduling; distributed locks; blue-green sync
workspaces and database swaps; template garbage collection; shared Git object
optimization; stacked feature branches; automatic sync on every main update; a
generic saga or resource-graph framework.

Deliberately kept small:

- **Schema-baseline hash** — `sha256` over sorted `(path, bytes)`. No parsing, no
  dependency graph.
- **Canonical repo fetch** — on demand, no scheduler.
- **Manifest** — extend `sandboxes`, no second table.
- **Lifecycle state** — five fields, no workflow engine.
- **Orphan handling** — startup reporting plus explicit retry, no reaper.

The existing startup audit plus explicit `resume` and `destroy` retries is enough
to converge state in v1.

---

## 10. Scope

### Build in v1

Deterministic identity and `sbx-<id>-*` naming; manifest-first lifecycle state
with ordered migrations; independent clones pinned to `origin/main`; one feature
branch per sandbox; both baseline commits; idempotent create, resume, sync,
reset-db, publish, and destroy; one database per sandbox with detected engine;
controller-owned branch push and PR creation; startup orphan reporting.

### Postpone

Git object sharing; database templates and template garbage collection;
blue-green sync; autonomous repair and cleanup loops; multi-host operation;
stacked sandboxes; shared installed dependency trees.

### First five steps

These are steps 1 to 5 of the table in section 6, restated. The numbering is the
same; there is no second ordering.

1. Ordered SQLite migrations, the lifecycle state fields, and the `projects`
   table rebuild.
2. Deterministic identity, resource naming, and sandbox-ID routes.
3. Extract the shared Git executor without changing behaviour.
4. Project-owned canonical mirror, two-step fetch, and `--no-local` clone
   creation, pinning both baselines.
5. Task and agent rows before sandbox work; preview row before Docker;
   delegation admission at revision creation; the lifecycle lease with crash
   reclaim; the project mirror lock; then converging `create`, `resume`, and
   `destroy` with draining.

Step 6 adds one database per sandbox with `reset-db`; steps 7 to 9 add sync,
publish with PR creation, and manifest-driven destroy. All of it is v1.

---

## 11. Decisions required before implementation

**Implementation gate.** Architecture review alone does not authorize code
changes. Record explicit acceptance of these decisions in an architecture
decision record (ADR). Then write a separate execution plan from section 6.
Accepting every recommendation below clears this gate. A different answer
requires an update to the affected design and dependency step first.

1. **Project source contract.** Recommended: new managed sandboxes require a Git
   remote with `origin/main`; folders without one stay legacy imports.
2. **Feature key source.** Who supplies it — the planning session, the human, or
   a slug of the feature title? It is the identity input and must be immutable.

   This is now blocking rather than open. A planning session cannot supply it
   under today's ordering: `planning/service.py:66 create_session` calls
   `ensure_sandbox_registered`, so a sandbox must already exist before a session
   does. Either the human supplies `feature_key` at create, or planning sessions
   must be able to start without a sandbox and create one on confirmation. The
   first is far cheaper and is the recommendation.
3. **Primary container meaning.** Recommended: one deterministic agent container
   per active sandbox, with preview, helper, and database containers as separate
   sidecars.
4. **Legacy delivery.** Recommended: keep the local source merge for legacy
   sandboxes; new sandboxes publish through the controller-owned PR path.
5. **Database engine.** Per-project detection, per section 8.
6. **Migration and seed commands for non-Prisma stacks.** Who supplies them, and
   in what form? `detection.py:506` knows only Prisma. Recommended: the
   detection record carries them, detection proposes a default set when it
   recognizes the stack, and `confirm-engine` requires them otherwise. They are
   controller-owned and never read from agent-editable files. This blocks step 6,
   not step 1.

---

## 12. Open questions

- Should the linked-worktree baseline failure (3.2) be fixed before step 4, or
  as part of step 4's legacy-import rejection? The preview record inversion is
  no longer open. Step 5 fixes it before lifecycle admission lands.
- How many dependency volumes exist today? It decides whether volume growth is a
  v1 concern.
- Does any current project rely on `shared_data` database mode?
- Which engines do the real target projects use today? Detection in 8.3 is worth
  building only if a second engine is actually coming.
- Should ADR 0003 and ADR 0005 be corrected now, or superseded when this lands?

---

## 13. Review verification

Inspected at commit `34b6040`. The backend suite was run during review:

```text
465 passed, 37 skipped, 1 warning in 12.53s
```

The skipped tests require Docker integration opt-in. No implementation changes
were made.
