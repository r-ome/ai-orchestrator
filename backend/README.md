# Backend

This directory contains the FastAPI backend. It uses controller-owned SQLite
for workflow state, approvals, and audit history. Docker and sandbox files
remain authoritative for runtime and code state.

## Install

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

## Run

```bash
uvicorn app.main:app
```

Managed agents and previews survive a backend restart. Startup reconciliation
matches SQLite records with labeled Docker resources. Docker restart policies
remain disabled, so applications do not restart automatically after a machine
reboot.

Controller settings:

```text
CONTROLLER_DATA_DIRECTORY       default: backend/.controller-data
PREVIEW_DEFAULT_EXPIRY_MINUTES  default: 30
PREVIEW_EXPIRY_POLL_SECONDS     default: 15
PREVIEW_INSPECTION_IMAGE        default: alpine:latest
PREVIEW_PREPARE_TIMEOUT_SECONDS default: 600
PREVIEW_BUILD_TIMEOUT_SECONDS   default: 900
PREVIEW_MAXIMUM_DEPENDENCY_BYTES default: 2147483648
PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES default: 4294967296
```

Open <http://127.0.0.1:8000/>. The response is `hello world`.

`GET /volumes` returns the named volumes and bind mounts attached to running
Docker containers. The backend must have access to the host Docker socket.

`GET /volumes/status` returns the host disk space used by Docker images,
containers, named volumes, and build cache. Docker does not count host bind-mount
data as Docker-managed storage.

Volume management routes:

- `GET /volumes/all` lists all Docker-managed volumes.
- `GET /volumes/{name}` inspects a volume and its container attachments.
- `GET /volumes/{name}/files?path=relative/file.txt` reads a file through a
  running container that mounts the volume. The maximum response is 1 MiB.
- `DELETE /volumes/{name}?confirm=true` permanently removes a volume.
- `POST /volumes/prune` removes unused volumes when the JSON body confirms it.
- `POST /volumes/{name}/containers/{id}/stop` stops an attached container when
  the JSON body confirms it.

The destructive request body is `{"confirm": true}`. The stop request also
accepts `timeout_seconds` from 1 through 60.

`GET /containers` returns the running Docker containers and their published
ports.

`GET /containers/status` returns one resource sample for each running container.
It includes CPU, memory, network I/O, block I/O, and process counts.

Container management routes:

- `GET /containers/all` lists running and stopped containers.
- `GET /containers/{id}` inspects configuration, mounts, networks, and state.
- `GET /containers/{id}/files?path=/absolute/file.txt` reads a regular file from
  the container filesystem. The maximum response is 1 MiB.
- `DELETE /containers/{id}?confirm=true` permanently removes a container.
- `POST /containers/prune` removes all stopped containers when confirmed.
- `POST /containers/{id}/stop` stops a running container when confirmed.

Container removal accepts `force=true` and `remove_volumes=true`. The volume
option removes anonymous volumes only. Named volumes remain intact.

## Project sandboxes

`POST /projects` creates the next sandbox for a host project folder. It returns
`202 Accepted`, creates an independent Docker volume, and starts a temporary
copy container.

```json
{
  "path": "/Users/jeromeagapay/Documents/my-project"
}
```

The API derives the project name from the selected folder. Repeated requests
for the same folder create `my-project-sandbox-1`,
`my-project-sandbox-2`, and so on. Every sandbox reads the original host folder.
It does not copy another sandbox volume.

Each response also contains an immutable `sandbox_id`. Docker labels and SQLite
use this ID even if a display name changes later.

The response includes a copy job ID and `status_url`. Copy status uses four
states: `queued`, `copying`, `completed`, and `failed`.

- `GET /projects/copies` lists all copy jobs.
- `GET /projects/copies/{job_id}` returns one job, its timestamps, exit code,
  Docker state, error, and up to the final 8,192 log bytes.
- `GET /projects` lists sandboxes and their current copy status.
- `GET /projects/{name}` inspects one sandbox.

Docker volume labels store sandboxes. The project volume stores final job
state and logs under `.orchestrator/copy-job`. Copy status does not depend on
SQLite. The state remains available after a backend restart.

Docker automatically removes the copy container when it exits. Status reads
use short-lived reader containers that also remove themselves. Deleting the
project volume deletes its copied files, final status, and logs together.

The default allowed root is `/Users/jeromeagapay/Documents`. Override it with
`PROJECTS_ROOT`. The API rejects the root itself, paths outside the root, and
paths that resolve outside it through a symbolic link.

The copy includes hidden files and preserves symbolic links without following
their targets. The copy helper uses the local `alpine:latest` image by default.
Override it with `PROJECT_COPY_IMAGE`.

The copy excludes dependency, environment, cache, coverage, and build folders
at any depth. The API returns the active list as `excluded_directories`.

```text
.next, .nox, .nuxt, .orchestrator, .parcel-cache, .pnpm-store, .pytest_cache,
.ruff_cache, .svelte-kit, .tox, .venv, .vite, __pycache__, bower_components,
build, coverage, dist, htmlcov, node_modules, site-packages, venv
```

## Feature delivery

A verified work item merges into the internal sandbox branch automatically.
The UI does not require a separate merge decision for each item.

A completed delegation exposes its accepted commit range through
`GET /projects/{name}/planning/sessions/{session_id}/delegations/{id}/diff`.
The response includes per-file totals and a unified patch. The patch stops at
500,000 bytes. Its base and head commits remain exact.

The feature review stores that same branch and commit range before its model
turn starts. The controller rejects the review result if the sandbox changes
during the turn.

The first delegated task also stores the sandbox's original dirty Git state.
Each entry includes Git status, file type, and a SHA-256 content fingerprint
when the path is a regular file or symbolic link. Diff generation, feature
review, and final merge allow unchanged pre-existing entries. They reject and
name every new, removed, status-changed, type-changed, or content-changed path.

Active sandboxes from older controller versions can have path-only task
baselines. The first delivery check upgrades that baseline only when all
current paths remain covered and all recorded paths still exist. Later checks
use the stored fingerprints and never ignore a directory by path alone.

`POST /projects/{name}/planning/sessions/{session_id}/delegations/{id}/changes`
accepts agent instructions against the complete implementation. The controller
keeps the prior implementation when the turn or full verification fails. A
successful change becomes another internal commit in `awaiting_review`. Its
prompt includes the reviewed plan, implementation manifest, completed work,
and earlier change requests. The agent must report observable acceptance
criteria and evidence. An approved whole-feature review marks held changes
complete. The controller retains the effective prompt, structured agent
report, turn measurements, and controller verification. Source delivery still
requires that current approved review.

Default coding images provide pinned Playwright and Chromium for behavioral
checks. Change turns use those image-owned tools and must not install browser
test infrastructure into a project or temporary directory.

`POST /projects/{name}/planning/sessions/{session_id}/delegations/{id}/merge`
fast-forwards the original source folder. The request must confirm the action
and name the latest approved review. The merge refuses a different source
branch, a changed source commit, any uncommitted source change, or a source
folder outside `PROJECTS_ROOT`. Git runs without network access or hooks.

## Preview stacks

Each sandbox supports zero or one active preview stack. A stack can use one of
three modes:

- `native` runs static HTML, Vite, Astro, Next.js, or FastAPI with a controlled
  image.
- `dockerfile` builds the current sandbox and runs the resulting image.
- `compose` builds or pulls every service and starts a multi-service stack.

Native Astro detection uses `astro.config.*` or the `astro` package dependency.
It uses `node:22-alpine`, installs locked npm dependencies, starts the `dev`
script on `0.0.0.0`, and routes container port 4321 to the preview gateway. The
controller disables Astro telemetry because native containers have a read-only
root filesystem.

The controller never executes detection results automatically. The flow is:

```text
inspect sandbox -> compare protected files -> review settings -> approve -> run
```

Preview routes:

- `POST /projects/{name}/preview-proposals` inspects the current volume.
- `GET /projects/{name}/preview-proposals/{id}/logs` returns creation progress.
- `POST /projects/{name}/previews` approves and starts or rebuilds a proposal.
- `GET /projects/{name}/previews/current` returns and keeps alive the stack.
- `POST /projects/{name}/previews/current/actions` reuses or restarts it.
- `POST /projects/{name}/previews/current/keep-alive` extends its expiry.
- `GET /projects/{name}/previews/current/logs` reads timestamped container logs.
- `DELETE /projects/{name}/previews/current` stops and removes the stack.

Preview creation writes ordered progress events to controller-owned SQLite and
the backend log. The frontend polls those events every 750 milliseconds. While
a preparation container runs, the response also includes its latest 200 output
lines. Failed creation logs remain available through the proposal route.

The frontend polls runtime logs every two seconds while Follow live is active.
It reads the latest 200 stdout and stderr lines from every preview container,
including every Compose service. Each response limits each container to 65,536
bytes. Logs written only to files inside a container do not appear here.

Protected runtime files include Compose files, Dockerfiles, dependency manifests,
lock files, Prisma schemas, Astro, Vite, and Next.js configuration, and
`.agent/preview.yaml`. The controller stores approved contents and hashes outside
the editable sandbox.
It rejects a start or restart when those files changed after inspection.

Compose mode starts every service but publishes only the approved service and
port. It rejects privileged mode, host networking, devices, added capabilities,
host bind mounts, `.env` files, secrets, configs, and controller-environment
interpolation. Relative `.:/target` mounts are translated to the sandbox volume.

An isolated preview uses an internal application network. A controller-owned
TCP gateway joins that network and a separate publishing network. Only the
gateway binds to `127.0.0.1`; project containers have no outbound route. Internet
mode gives project containers outbound access after explicit approval.

Dependency installation and image builds use normal outbound access. Runtime
isolation starts after preparation. Preview-created data volumes are temporary
unless their logical names appear in `persistent_volumes`.

Native inspection also detects Prisma schemas whose datasource provider is
MySQL. It proposes a controller-managed database but does not start it. The
approved native settings can define:

```yaml
services:
  database:
    type: mysql
    image: mysql:8.4
    database: atc_preview
    persistence: ephemeral
initialize:
  commands:
    - npx prisma migrate deploy
    - npm run db:seed:preview
environment:
  DATABASE_URL:
    from_service: database
```

The controller creates random MySQL credentials and injects `DATABASE_URL` into
the initialization and application containers. MySQL has no published host
port. The controller copies native database previews into a runtime workspace
that excludes `.env` and `.env.local` files before dependency installation.

Start waits for MySQL health, then runs the approved project migration and seed
commands in a separate initialization container. The application starts only
after that container exits successfully. Database, initialization, and
application output remain available through preview logs.

Restart keeps the database volume and does not run initialization again.
Rebuild creates new ephemeral database data and runs initialization again. Stop
and expiry always remove ephemeral database data. Persistent database data and
its credentials remain only when the approved database setting explicitly uses
`persistence: persistent`.

Default expiry is 30 minutes. Opening the preview record, viewing logs, reusing,
restarting, rebuilding, or pressing Keep running records activity. The backend
stops overdue stacks while running and immediately after a later restart.

## Coding agents

Build the Claude Code and Codex images before you summon an agent. See
[`agent-images/README.md`](agent-images/README.md).

`GET /agents/providers` lists the available providers and configured images.
The default providers are `claude` and `codex`.

`POST /agents` creates and starts one disposable agent container:

```json
{
  "project_name": "my-project",
  "provider": "claude",
  "credential_profile": "personal"
}
```

The project must have a completed copy job. The response contains the agent ID
and `websocket_url`. A sandbox allows only one active coding agent.

`POST /agents/{id}/replace` requires `confirm=true`. It stops the current agent,
preserves sandbox and credential volumes, and starts the requested replacement.
The API never silently replaces an agent.

Each agent container gets these writable mounts:

```text
/workspace  -> the selected project sandbox volume
/auth       -> the provider credential-profile volume
/tmp        -> an in-memory temporary filesystem
```

Claude receives `CLAUDE_CONFIG_DIR=/auth`. Codex receives `CODEX_HOME=/auth`.
The API creates credential volumes when required. The same provider and profile
reuse the same volume. Claude and Codex never share one credential volume.

Credentials do not enter the image, project volume, API body, or container
command. The container does not mount the Docker socket or a host home folder.
It uses `/tmp/home` as an ephemeral home directory for other CLI cache files.

Connect to `websocket_url` to start the selected interactive CLI inside a
`tmux` session. The first session presents the provider's login flow. Complete
the displayed link or device code in a separate host-browser tab. Later agents
reuse the saved login state.

The WebSocket uses the `terminal.v1` protocol:

| Direction | Frame | Meaning |
| --- | --- | --- |
| Server to client | JSON `ready` | The terminal process started. |
| Server to client | Binary | Terminal output, including ANSI control bytes. |
| Client to server | Binary | Terminal keyboard input. |
| Client to server | JSON `input` | UTF-8 input in the `data` field. |
| Client to server | JSON `resize` | TTY size in `columns` and `rows`. |
| Client to server | JSON `close` | Detach this terminal connection. |
| Server to client | JSON `exit` | The CLI process exited. |
| Server to client | JSON `error` | The request or terminal operation failed. |

Example control messages:

```json
{"type":"input","data":"yes\r"}
{"type":"resize","columns":120,"rows":40}
{"type":"close"}
```

One agent supports one active WebSocket. Closing the WebSocket detaches its
terminal. The container and `tmux` session keep running. Reconnect to the same
`websocket_url` to reattach and see the current terminal.

`POST /agents/{id}/stop` stops the container when the body confirms the action.
`GET /agents` and `GET /agents/{id}` inspect active agents.

Docker automatically removes explicitly stopped agent containers. Backend
startup and shutdown do not remove active agents, project volumes, or credential
volumes.

Run one backend worker. WebSocket ownership and in-process creation locks remain
process-local. Durable lifecycle intent and approvals remain in SQLite.

Image overrides:

```text
CLAUDE_AGENT_IMAGE=orchestrator-agent-claude:latest
CODEX_AGENT_IMAGE=orchestrator-agent-codex:latest
```

## Test

```bash
pytest
```

The normal suite skips Docker integration tests. Run all preview modes against
the local Docker daemon with:

```bash
RUN_DOCKER_PREVIEW_TESTS=1 pytest tests/previews/test_docker_integration.py
```
