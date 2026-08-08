# Tech spec: task branches and gated previews

Implements `docs/adr/0002-previews-run-from-sandbox-commits.md` and
`docs/adr/0003-controller-owned-dependency-volumes.md`.
Phase order and exit criteria live in `task-preview-plan.md`.

This spec is written to be executed by an agent that has not read the
codebase. Every anchor below was verified against the working tree. Line
numbers may drift; the function names are authoritative.

---

## 1. Current behaviour

Read these before changing anything.

| Anchor | What it does |
|---|---|
| `backend/app/previews/service.py:1118` `_copy_native_workspace` | Tars the sandbox volume into a fresh run-scoped `runtime-workspace` volume, excluding `.env`, `.env.local`, `.orchestrator` |
| `backend/app/previews/service.py:847` | The only call site. Runs once, from the native start path |
| `backend/app/previews/service.py:502` `restart_preview` | Restarts containers only. Never re-copies |
| `backend/app/previews/service.py:860` | Creates the `node-modules` volume scoped to `run_id` |
| `backend/app/previews/service.py:3015` `_data_volume` | Names volumes. `persistent=True` gives a sandbox-scoped name, `False` gives a run-scoped one |
| `backend/app/previews/service.py:751` `_record_preview_progress` | Writes a `preview.progress` event into the controller store. No timing |
| `backend/app/projects/service.py:44` `COPY_METADATA_DIRECTORY` | `/project/.orchestrator/copy-job`, inside the agent-writable volume |
| `backend/app/projects/service.py:67` | `node_modules` in `EXCLUDED_DIRECTORY_NAMES` for the host import |
| `backend/app/agents/service.py:63` `create_agent` | Creates the agent container. Mounts the sandbox volume and the credential volume |
| `backend/app/controller/store.py:161` `initialize` | Runs `SCHEMA` then records migration versions |
| `backend/app/controller/store.py:670` `event`, `:687` `events_for_run` | The event store |
| `backend/app/agents/router.py:154` | WebSocket pattern to copy for log streaming |

Verified failure this work fixes: the sandbox volume holds
`src/pages/contact.astro`; the preview `runtime-workspace` volume does not.
The sandbox `node_modules` holds 0 entries; the preview `node-modules` volume
holds 396 and contains `astro`.

---

## 2. Phase 0 — Git baseline in the sandbox

**Where:** `backend/app/projects/service.py`

After the host copy completes, ensure the sandbox volume is a git repository.
Run in a throwaway container, the same hardened pattern
`_copy_native_workspace` already uses (`remove=True`, `network_disabled=True`,
`cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`):

```sh
set -eu
cd /project
if [ ! -d .git ]; then
  git init -q -b main
fi
git config user.name "orchestrator"
git config user.email "orchestrator@localhost"
if ! git rev-parse HEAD >/dev/null 2>&1; then
  git add -A
  git commit -q -m "sandbox baseline" --allow-empty
fi
```

**Blocker, verified.** The inspection image is `alpine:latest`
(`previews/config.py:29`) and it has no git:

```
$ docker run --rm alpine:latest sh -c 'command -v git || echo "NO GIT"'
NO GIT
```

Git containers run with `network_disabled=True`, so `apk add git` cannot work
at runtime. Add a separate setting beside `inspection_image`:

```python
git_image: str = os.getenv("PREVIEW_GIT_IMAGE", "alpine/git:latest")
```

Pull it through the existing `_ensure_image` helper. Every git container in
this spec uses `git_image`, not `inspection_image`. Note that `alpine/git`
sets an `ENTRYPOINT` of `git`, so pass `entrypoint=["sh", "-c"]` when running
the shell snippets below.

Record the baseline commit on the sandbox row. Add a nullable
`baseline_commit TEXT` column to `sandboxes` (see section 8 for how to
migrate).

**Do not** rewrite history, touch `origin`, or run any network git command.
A sandbox that already has commits keeps them; only the branch pointer and
the identity config are set.

---

## 3. Phase 1 — Dependency volume keyed by lockfile

**Where:** `backend/app/previews/service.py`, `backend/app/agents/service.py`

### Naming

Add a helper next to `_data_volume`:

```python
def _dependency_volume_name(sandbox_id: str, lockfile_digest: str) -> str:
    return f"orchestrator-deps-{sandbox_id[:12]}-{lockfile_digest[:12]}"
```

`lockfile_digest` is the SHA-256 of the first lockfile found in the sandbox
volume root, checked in this order: `package-lock.json`, `pnpm-lock.yaml`,
`yarn.lock`, `uv.lock`, `poetry.lock`, `requirements.txt`. If none exists,
digest the string `"none"`. Read the file with the existing volume-read helper
used by `_volume_runtime_files`; do not add a new read path.

### Lifecycle

Replace the `run_id`-scoped `node-modules` volume at
`backend/app/previews/service.py:860` with the name above. Label it the same
way `_data_volume` labels persistent volumes so cleanup still recognises it.

Mount rules, enforced everywhere:

| Container | Mode |
|---|---|
| Install job (`_run_prepare`) | `rw` |
| Preview app container | `ro` |
| Agent container | `ro` |

If the volume already exists and is non-empty, skip the install command
entirely and emit a progress event with `duration_ms: 0`.

### Agent mount

In `create_agent`, add the dependency volume to the `volumes` mapping at
`/workspace/node_modules` with `"mode": "ro"`. The agent container is created
before any preview exists, so the volume may be absent — create it empty in
that case rather than failing agent creation.

**Known risk:** some tools write into `node_modules` (Vite writes
`node_modules/.vite`). If the preview app container fails on a read-only
mount, give the preview `rw` and keep the agent `ro`. Do not give the agent
`rw` — that is the authority boundary this ADR exists to draw.

---

## 4. Phase 2 — Task records and task branches

**Where:** `backend/app/controller/store.py`, new `backend/app/tasks/`

### Schema

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    agent_run_id TEXT,
    branch TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    head_commit TEXT,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_task_per_sandbox
ON tasks(sandbox_id)
WHERE status IN ('open', 'reported', 'previewing', 'review');
```

The partial unique index mirrors `one_active_agent_per_sandbox`
(`store.py:47`) and is what enforces the single-task-per-sandbox rule from the
plan. Rely on the `sqlite3.IntegrityError` it raises; do not add a Python-side
check.

### Status machine

Corrected during Phase 2. `TASK_TRANSITIONS` in `backend/app/tasks/models.py`
is the authority; this table mirrors it.

| From | To | Trigger |
|---|---|---|
| `open` | `reported` | Controller reads a branch head that moved |
| `open` | `rejected` | **Phase 4 must add this.** See below |
| `reported` | `previewing` | Controller starts the task preview |
| `previewing` | `review` | Preview is serving |
| `previewing` | `failed` | Preview build failed |
| `review` | `accepted` / `rejected` / `failed` | Human decision |

`open` is the only status an agent's actions can reach. Every other
transition is a controller action. `accepted`, `rejected`, and `failed` are
terminal.

Two edges exist because omitting them deadlocks a sandbox. Every non-terminal
status sits inside `one_open_task_per_sandbox`, so a task with no exit blocks
its sandbox from ever opening another task:

- `previewing → failed` — without it, a failed Phase 3 build strands the task.
- `open → rejected` — without it, an agent that never commits blocks the
  sandbox forever. **This edge is still missing.** Phase 4 owns reject and
  must add it to `TASK_TRANSITIONS`; it is a one-line change.

Implement `transition_task` by destination, never by source: derive the
permitted sources from the table and let the store apply them as
`UPDATE ... WHERE id = ? AND status IN (...)`. That makes the check atomic
instead of read-then-write, and stops a caller naming its own source.

### Branch creation

Corrected during Phase 2. The original ordering here ran git before the task
row existed, so a caller that then lost the unique-index race had already
switched the sandbox onto a stray branch — and rolling that back would mutate
a sandbox holding somebody else's live task.

Order is load-bearing:

1. `ensure_git_baseline` returns the current `HEAD`. That is `base_commit`.
2. Insert the task row. The unique index is the lock.
3. Only then create the branch, guarded against a moved head:

```sh
set -eu
cd /project
head="$(git rev-parse --verify HEAD)"
if [ "$head" != "<base_commit>" ]; then
  echo "sandbox head moved to $head" >&2
  exit 1
fi
git switch -c "task/<task_id>"
```

4. If branch creation fails, delete the row so the slot frees.

`base_commit` must come from live `HEAD`, never from `sandboxes.baseline_commit`.
That column holds the *first* commit ever made; a second task has to branch
from wherever `HEAD` is now.

### Completion signal

The agent reports completion through the API. Treat that report as a hint
only. The controller then reads the branch head itself:

```sh
git -C /project rev-parse "refs/heads/task/<task_id>"
```

A task moves to `reported` only when that head differs from `base_commit`.
An unchanged head returns `409`: a completion claim with nothing to review is
the same class of conflict as a dirty tree.

Reject the report with `409` if `HEAD` is not the task branch. Otherwise
`git status --porcelain` describes the wrong worktree and the dirty-tree check
below can be satisfied by an agent simply switching branches.

**Do not** read any file the agent wrote to decide task state. Specifically,
`/workspace/.agent/preview.yaml` is an untrusted proposal, consistent with the
"Preview proposal" definition in `CONTEXT.md`.

Uncommitted work is not a task result. If the agent reports done with a dirty
tree, return `409` naming the dirty paths. Do not auto-commit on the agent's
behalf.

---

## 5. Phase 3 — Preview from a commit

**Where:** `backend/app/previews/service.py`

Two preview kinds now exist. Add a `kind` column to `preview_runs`
(`'live' | 'task'`), plus nullable `task_id TEXT` and `commit_sha TEXT`.

### Live preview

Mount the sandbox volume directly at `/workspace`, with the dependency volume
overlaid at `/workspace/node_modules`. Do not call `_copy_native_workspace`.

The `.env` exclusion the copy provided must be preserved another way. Real
values continue to arrive through `_secret_environment`.

**Corrected during Phase 3.** An earlier version of this spec said to mount an
empty tmpfs file over each path. There is no such thing — a tmpfs mount is a
directory, and Docker refuses to mount one over a regular file:

```
$ docker run --rm -v $V:/w --tmpfs /w/.env alpine:latest sh -c 'cat /w/.env'
not a directory: Are you trying to mount a directory onto a file (or vice-versa)?
```

That failure mode is the worst available: it succeeds while no `.env` exists
and hard-fails the preview exactly when one does — precisely when the mask is
needed.

Bind `/dev/null` over each path instead. It is a character device, so Docker
binds it over an existing regular file and creates an empty file when the
target is absent, covering both cases with one mechanism. It resolves on the
daemon host, so no controller-owned host path is required and a remote daemon
still works. Because docker-py keys its `volumes` dict by source, two masks
collide on the `/dev/null` key — pass them through `mounts=[Mount(...)]`,
which the daemon merges with `Binds` and `Tmpfs`.

Docker materialises a missing bind target inside the underlying volume, so
masking a `.env` that does not exist writes a real empty `.env` into the
sandbox. Untracked, that trips Phase 2's dirty-tree rule. Append `.env` and
`.env.local` to `.git/info/exclude` in the sandbox — local ignore state, not
history, and not part of the worktree.

**Known limitation — reviewed and accepted.** Root `.env` and `.env.local` are masked whether or not
they exist, so an agent cannot defeat the mask by creating one after start.
Deeper paths are masked only where a file already sits at preview start; a
`.env` inside a directory created after the preview starts is readable by the
preview runtime. Static mounts cannot mask recursively by name, and the
alternatives that can (an AppArmor path profile) are unavailable on Docker
Desktop's LinuxKit VM. Task previews have no such gap — `_export_commit`
deletes env files at every depth into a controller-owned workspace volume.

Accepted as a live-preview-only risk rather than blocking the design. Revisit
only if live previews start defaulting to `PreviewNetworkAccess.INTERNET`,
which is what turns a readable env file into an exfiltration path.

The app already runs `npm run dev -- --host 0.0.0.0`, so an agent edit reaches
the browser through hot module replacement with no restart.

### Task preview

Replace `_copy_native_workspace` with `_export_commit`, same container
hardening, same volume pair:

```sh
set -eu
git -C /source archive --format=tar "<commit_sha>" | tar -C /workspace -xf -
rm -f /workspace/.env /workspace/.env.local
```

`git archive` gives three properties the tar copy lacked: it is reproducible,
it excludes `.git`, and it excludes everything the repository ignores.

### Restart

In `restart_preview` (`service.py:502`), a task preview must re-export its
recorded `commit_sha` before restarting containers. A live preview needs no
export. This is the direct fix for "restart does not rebuild".

### Approval digest

For a task preview, the approved digest is
`proposal_digest(config, {"commit": commit_sha})`. Leave the existing
protected-file hash path untouched for live previews to keep blast radius
small.

---

## 6. Phase 4 — Accept and reject

**Where:** new `backend/app/tasks/service.py`

### Accept

**Corrected during Phase 4.** Earlier versions of this section said
`git switch main`. Nothing guarantees a sandbox is on `main`.
`ensure_git_baseline` runs `git init -b main` only when `.git` is absent, and
`.git` is **not** in `EXCLUDED_DIRECTORY_NAMES`, so a host folder that is
already a repository is imported whole and keeps its own branch — `master`,
`develop`, anything. On such a sandbox `git switch main` fails with "invalid
reference", breaking accept *and* reject, which would make the `open →
rejected` deadlock fix unreachable on exactly those sandboxes.

`start_task` reads `git symbolic-ref --quiet --short HEAD` before cutting the
task branch and records it as `tasks.base_branch` — trusted metadata, so the
branch name is never re-derived from sandbox state an agent can change. A
detached HEAD is refused at start rather than producing a task that can never
settle.

```sh
set -eu
cd /project
git switch "<base_branch>"
git merge --ff-only "task/<task_id>"
git branch -d "task/<task_id>"
```

Check every refusal condition — base branch exists, branch head still equals
the reviewed `head_commit`, worktree clean, fast-forward possible — *before*
touching the checkout. The literal three-line script above leaves the sandbox
switched onto the base branch after a failed merge; refusing first does not.

`--ff-only` is deliberate. If the sandbox branch moved since the task started,
the merge fails and the task stays in `review`. Return `409` with the
divergence, and let the human decide. Do not merge, rebase, or force anything
automatically — this is the one place in the system that can destroy committed
work.

On success: task to `accepted`, `settled_at` set, task preview stopped, its
run-scoped volumes removed. The dependency volume survives; it is keyed by
lockfile, not by run.

### Reject

```sh
set -eu
cd /project
git switch "<base_branch>"
git branch -D "task/<task_id>"
```

Reject must **not** require a clean worktree — it exists precisely for the
agent that never committed. Uncommitted files travel with the switch, and
where git refuses because they would be overwritten, return `409` naming
them. Never discard them.

Task to `rejected`. The sandbox branch is untouched by construction.

Add `open → rejected` to `TASK_TRANSITIONS` as part of this phase. Reject must
work on a task that never reached `review` — otherwise a coding agent that
never commits leaves its sandbox permanently unable to open another task.
When the branch was never created, `git branch -D` fails harmlessly; treat a
missing branch as success rather than an error.

Both paths are idempotent. Re-running accept on an accepted task returns the
existing result rather than erroring.

---

## 7. Phase 5 — Timing and live logs

**Where:** `backend/app/previews/service.py:751`, `backend/app/previews/router.py`

### Timing

Partly landed in Phase 1: `_record_preview_progress`, `_ignore_progress`, and
the `progress` closure in `start_preview` already accept an optional
`duration_ms`, because the dependency-reuse path needs to report zero. Extend
rather than re-add.

Extend `_record_preview_progress` with optional `started_at: str | None`, and
include it in the event payload alongside the existing `duration_ms`. Add a small
context manager beside it:

```python
@contextmanager
def _timed_step(report, step: str, message: str):
    ...
```

It emits a start event, times the block, and emits a completion event
carrying `duration_ms`. Wrap these four operations with it:

- workspace export or clone
- dependency install
- build check
- application container start to first successful health probe

### Streaming

Add `GET /previews/{project_name}/events` as a WebSocket, copying the
structure of `backend/app/agents/router.py:154`. Replay recent events with
`events_for_run` (`store.py:687`), then push new ones as they arrive.

Also stream the check-job container logs. `docker_terminal.read_stream`
already handles Docker's multiplexed stream format; reuse it rather than
writing a new reader.

---

## 8. Phase 6 — Metadata move and container limits

**Where:** `backend/app/projects/service.py`, `backend/app/agents/service.py`

Move copy-job metadata out of the agent-writable volume. Today
`COPY_METADATA_DIRECTORY` is `/project/.orchestrator/copy-job`
(`projects/service.py:44`), so an agent can rewrite copy status and mislead
the controller. Mount a separate controller-owned volume at `/controller` in
the copy container and write there instead. Update the readers at
`projects/service.py:478-486`.

The original instruction to "drop `.orchestrator` from the preview exclusion
list" no longer applies: Phase 3 deleted `_copy_native_workspace` and its
exclusion list along with it. `_export_commit` uses `git archive`, which skips
ignored and untracked paths by construction. No preview change is needed here.

In `create_agent`, add `pids_limit=512` and `mem_limit` from agent settings.
The container already sets `read_only=True`, `cap_drop=["ALL"]`, and
`security_opt=["no-new-privileges:true"]`; do not change those.

---

## 9. Schema migrations

`ControllerStore.initialize` (`store.py:161`) runs `SCHEMA` with
`CREATE TABLE IF NOT EXISTS` and then records version rows. Follow that
pattern: add new tables to `SCHEMA`, add new columns with a guarded
`ALTER TABLE` that ignores "duplicate column name", and insert version `3`.
An existing `controller.sqlite3` must survive the upgrade — there is a live
one at `backend/.controller-data/controller.sqlite3`.

---

## 10. Rules for the implementing agent

- **Never run a git command that mutates the whole working tree.** No
  `git stash`, `git stash pop`, `git checkout .`, `git restore .`,
  `git reset --hard`, `git clean`, or `git revert`. Phases run concurrently in
  one shared tree, so these revert other agents' uncommitted, unpushed work —
  including work that exists nowhere else. A Phase 4 agent ran `git stash` /
  `git stash pop` while Phase 5 was mid-write; the pop happened to restore
  cleanly, but a conflict would have destroyed three phases of uncommitted
  work with no recovery path. To inspect a change, read the file. To compare
  against `HEAD`, use `git diff` or `git show`, which mutate nothing. If you
  believe you need to revert something, stop and report it instead.
- Do not touch `PreviewMode.COMPOSE` or `PreviewMode.DOCKERFILE` paths.
- Do not add `git worktree`. Single task per sandbox is the agreed scope.
- Do not run any git command that reaches the network. Every git container
  keeps `network_disabled=True`, which also rules out installing git at
  runtime — use the `git_image` setting from section 2.
- Do not let an agent-written file influence controller state.
- Do not weaken the existing container hardening flags anywhere.
- Match surrounding style: keyword-only arguments in store methods, `StrEnum`
  for new enums, docstrings only where the reason is not obvious from the
  name — see the `PreviewSharing` docstring in `previews/models.py` for the
  register to match.
- Every phase adds tests under `backend/tests/`. Run `uv run pytest` from
  `backend/` before reporting done.
