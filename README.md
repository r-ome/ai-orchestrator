# Orchestrator

Orchestrator creates isolated coding sandboxes from host folders. It also runs coding agents and controlled application previews inside Docker.

The project provides a React control panel and a FastAPI controller. The controller manages sandbox copies, preview approvals, Docker resources, and agent terminals.

## What it does

- Copies a host project folder into an independent Docker volume.
- Keeps each sandbox separate from its source folder and sibling sandboxes.
- Shows a commit-pinned feature diff before delivery to the source folder.
- Fast-forwards approved features into a clean, unchanged source branch on request.
- Runs one writable Claude Code or Codex session in each sandbox.
- Detects and proposes preview settings before it runs project code.
- Supports native, Dockerfile, and Docker Compose previews.
- Requires explicit approval when protected runtime files or preview settings change.
- Stores preview secrets outside the editable sandbox.
- Supports isolated, shared-server, and shared-data MySQL preview modes.
- Shows Docker containers, mounts, managed volumes, processes, and storage use.
- Provides browser terminals for coding agents and running containers.

## Architecture

```text
Browser
  |
  | HTTP and WebSocket
  v
React + Vite (:5173)
  |
  | /api proxy during development
  v
FastAPI controller (:8000) ------> controller-owned SQLite
  |                                 approvals, lifecycle intent, audit state
  |
  | Docker API
  v
Docker daemon
  |-- sandbox volumes
  |-- coding-agent containers and credential volumes
  |-- preview containers, networks, gateways, and data volumes
  `-- temporary copy and inspection containers
```

Docker and sandbox files remain authoritative for runtime and code state. SQLite stores policy that sandbox code cannot change.

The controller reconciles SQLite with labeled Docker resources when it starts. It does not automatically restart containers after a machine reboot.

## Requirements

- Docker Engine or Docker Desktop with a running daemon.
- Python 3.11 or later.
- [`uv`](https://docs.astral.sh/uv/) for the documented backend workflow.
- Node.js `^20.19.0`, `^22.13.0`, or `>=23.5.0`.
- npm.

Docker Desktop must have access to the host folders under `PROJECTS_ROOT`.

## Quick start

### 1. Start the backend

Run these commands from the repository root:

```bash
cd backend
uv sync --extra test
PROJECTS_ROOT="/absolute/path/to/your/projects" \
  uv run uvicorn app.main:app --reload
```

Set `PROJECTS_ROOT` to the parent folder that users may browse and copy. The API rejects the root itself and paths outside it.

The backend starts at <http://127.0.0.1:8000>. Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

You can use a standard virtual environment instead of `uv`:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
uvicorn app.main:app --reload
```

### 2. Start the frontend

Open a second terminal at the repository root:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>. Vite forwards `/api` requests and WebSocket connections to the backend.

### 3. Build coding-agent images

This step is optional unless you want to run a coding agent.

```bash
cd backend
docker build \
  --file agent-images/claude/Dockerfile \
  --tag orchestrator-agent-claude:latest \
  agent-images/claude

docker build \
  --file agent-images/codex/Dockerfile \
  --tag orchestrator-agent-codex:latest \
  agent-images/codex
```

These builds download Debian packages and npm packages. Agent sessions connect to their provider and may require a paid subscription or API account.

## First workflow

1. Open **Projects**.
2. Select a folder below `PROJECTS_ROOT`.
3. Create a sandbox and wait for its copy job to finish.
4. Open the sandbox detail page.
5. Summon a coding agent, or inspect a preview proposal.
6. Review the proposed settings and protected-file changes.
7. Approve and start the preview.
8. Stop unused previews, agents, and Docker resources from the control panel.

A sandbox is a one-time snapshot. Later source-folder changes do not sync into it.

The source folder changes only when a person confirms an approved feature merge.
That action is explicit. It is not synchronization.

Delegated work items merge automatically inside the sandbox after controller
verification. Review remains at feature level. A person can request small
agent changes and rerun verification before approving one final source merge.
Each change stays awaiting review until an independent whole-feature review
approves the current sandbox commit.

Repeated copies of one source folder create numbered sandboxes. Each copy always reads the original host folder.

## Core concepts

| Term | Meaning |
| --- | --- |
| Sandbox | An editable snapshot with a stable identity and a dedicated Docker volume. |
| Coding agent | The single writable automation session assigned to a sandbox. |
| Reviewer | A read-only participant that can inspect a sandbox. |
| Preview proposal | A non-executable suggestion for running the current sandbox. |
| Approval | Human permission to run one exact preview proposal revision. |
| Preview stack | The active application runtime for a sandbox. |
| Protected runtime file | A file change that invalidates an earlier preview approval. |
| Source merge | An explicit fast-forward of one approved feature commit into the original project folder. |
| Shared database server | One MySQL container that can serve several sandboxes of one source project. |
| Database guest | A sandbox that writes to another sandbox's schema. |
| Trusted metadata | Controller-owned workflow and audit data that project code cannot modify. |

See [`CONTEXT.md`](CONTEXT.md) for the complete domain language.

## Sandboxes

The controller copies each selected folder into a labeled Docker volume. Hidden files and symbolic links are preserved.

Symbolic links are copied without following their targets. The controller rejects paths that escape `PROJECTS_ROOT` through a symbolic link.

The copy skips dependency, virtual-environment, cache, coverage, and build folders at every depth. Important exclusions include:

```text
.venv, .next, .nuxt, .pytest_cache, .tox, .vite, __pycache__,
build, coverage, dist, node_modules, site-packages, and venv
```

Copy state and the final 8,192 log bytes live inside the sandbox volume. Status remains available after a backend restart.

Deleting the sandbox volume deletes its files, copy state, and copy logs.

## Coding agents

Orchestrator supports `claude` and `codex` providers by default. A sandbox permits one active coding agent.

Each agent container receives three writable locations:

```text
/workspace  sandbox volume
/auth       provider-specific credential-profile volume
/tmp        in-memory temporary filesystem
```

The container has a read-only root filesystem. It drops Linux capabilities and does not mount the Docker socket or host home folder.

The first terminal connection starts the provider CLI in `tmux`. Complete the provider login flow in a separate browser tab.

Later agents can reuse the same provider and credential profile. Claude and Codex never share a credential volume.

Closing the browser terminal detaches it. The container and `tmux` session continue until you stop the agent.

Rebuild the agent image after its Dockerfile changes. See [`backend/agent-images/README.md`](backend/agent-images/README.md) for more detail.

## Preview safety model

The controller uses this approval flow:

```text
inspect sandbox -> compare protected files -> review settings -> approve -> run
```

It never runs a detection result automatically. It rejects start or restart when protected files change after inspection.

Protected files include dependency manifests, lock files, Compose files, Dockerfiles, Prisma schemas, and framework configuration files.

Preview modes include:

| Mode | Behavior |
| --- | --- |
| `native` | Runs detected static HTML, Vite, Astro, Next.js, or FastAPI code in a controlled image. |
| `dockerfile` | Builds the current sandbox and runs the resulting image. |
| `compose` | Builds or pulls all services, then publishes only the approved service and port. |

Isolated previews have no outbound route after preparation. A controller gateway binds the approved service to `127.0.0.1`.

Internet mode gives preview containers outbound access after explicit approval. Dependency installation and image builds use outbound access during preparation.

Compose mode rejects privileged mode, host networking, devices, added capabilities, host bind mounts, secrets, configs, and `.env` files.

Preview stacks expire after 30 minutes by default. Viewing, restarting, rebuilding, reusing, or keeping a preview alive records activity.

## Preview secrets and databases

The project detail page can store named preview secrets. Secret values remain outside the editable sandbox.

The controller can import supported variables from sandbox environment files. It shows secret names and timestamps without returning stored values.

Native previews can use a controller-managed MySQL service. The controller generates credentials and injects `DATABASE_URL` into approved containers.

Database sharing has three modes:

| Mode | Server | Schema and data |
| --- | --- | --- |
| `isolated` | One server per sandbox preview. | Private to the sandbox. |
| `shared_server` | One server per source project. | Each sandbox owns a separate schema. |
| `shared_data` | One server per source project. | A database guest uses another sandbox's schema. |

Ephemeral database data disappears when its preview stops. Persistent data remains only when the approved configuration selects persistent storage.

## Docker management

The control panel includes these views:

- **Containers** shows running and stopped containers, processes, ports, mounts, and resource samples.
- **Mounts** shows volumes and bind mounts attached to running containers.
- **Managed volumes** inspects Docker volumes and reads files through an attached container.
- **Storage status** shows Docker image, container, volume, and build-cache use.

Container and volume removal is destructive. The API requires explicit confirmation for stop, remove, and prune operations.

Docker storage totals do not include data in host bind mounts.

## Configuration

Set backend variables in the environment before the backend starts.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROJECTS_ROOT` | `/Users/jeromeagapay/Documents` | Limits folders that users can browse and copy. Set this on other hosts. |
| `PROJECT_COPY_IMAGE` | `alpine:latest` | Copies source folders into sandbox volumes. |
| `CONTROLLER_DATA_DIRECTORY` | `backend/.controller-data` | Stores controller SQLite state. |
| `CLAUDE_AGENT_IMAGE` | `orchestrator-agent-claude:latest` | Selects the Claude Code agent image. |
| `CODEX_AGENT_IMAGE` | `orchestrator-agent-codex:latest` | Selects the Codex agent image. |
| `PREVIEW_INSPECTION_IMAGE` | `alpine:latest` | Reads sandbox files during preview inspection. |
| `PREVIEW_DEFAULT_EXPIRY_MINUTES` | `30` | Sets the proposed preview lifetime. |
| `PREVIEW_EXPIRY_POLL_SECONDS` | `15` | Sets the controller expiry-check interval. |
| `PREVIEW_MAXIMUM_FILE_BYTES` | `1048576` | Limits each protected file captured during inspection. |
| `PREVIEW_MAXIMUM_SNAPSHOT_BYTES` | `16777216` | Limits the total protected-file snapshot size. |
| `PREVIEW_PROPOSAL_LIFETIME_SECONDS` | `900` | Sets how long an unapproved proposal remains valid. |
| `PREVIEW_PREPARE_TIMEOUT_SECONDS` | `600` | Limits dependency preparation time. |
| `PREVIEW_BUILD_TIMEOUT_SECONDS` | `900` | Limits image build time. |
| `PREVIEW_MAXIMUM_DEPENDENCY_BYTES` | `2147483648` | Limits prepared dependency volume size. |
| `PREVIEW_MAXIMUM_BUILT_IMAGE_BYTES` | `4294967296` | Limits a built preview image size. |
| `PREVIEW_MEMORY` | `4g` | Sets the memory limit for each preview workload. |
| `PREVIEW_SHARED_DATABASE_MEMORY` | `2g` | Sets the shared MySQL server memory limit. |
| `PREVIEW_SHARED_DATABASE_MAX_CONNECTIONS` | `200` | Sets the shared MySQL connection limit. |

The frontend supports one build-time or startup variable:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE` | `/api` | Sets the backend base URL used by browser requests. |

The development proxy expects the backend at `http://127.0.0.1:8000`.

## API overview

FastAPI publishes the complete request and response schemas at `/docs` and `/openapi.json`.

| Prefix | Responsibility |
| --- | --- |
| `/projects` | Browse source folders, create sandboxes, and inspect copy jobs. |
| `/projects/{name}/preview-proposals` | Inspect code and create a reviewable preview proposal. |
| `/projects/{name}/previews` | Start, inspect, update, and stop preview stacks. |
| `/projects/{name}/secrets` | Manage preview secret names and values. |
| `/projects/{name}/planning/sessions/{id}/delegations` | Run work items, review the feature diff, and merge approved code into the source folder. |
| `/agents` | List providers and manage coding-agent containers. |
| `/containers` | Inspect, monitor, stop, remove, prune, and open container shells. |
| `/volumes` | Inspect mounts, managed volumes, files, and Docker storage use. |

Terminal endpoints use WebSockets. Agent terminals use the `terminal.v1` frame protocol documented in [`backend/README.md`](backend/README.md).

## Development commands

Run backend tests:

```bash
cd backend
uv run pytest
```

The normal suite skips Docker preview integration tests. Run those tests explicitly against a local Docker daemon:

```bash
cd backend
RUN_DOCKER_PREVIEW_TESTS=1 \
  uv run pytest tests/previews/test_docker_integration.py
```

Check and build the frontend:

```bash
cd frontend
npm run lint
npm run build
```

The backend must run as one worker. Terminal ownership and creation locks are process-local.

## Repository layout

```text
.
|-- backend/
|   |-- app/
|   |   |-- agents/       coding-agent lifecycle and terminals
|   |   |-- containers/   Docker container inspection and actions
|   |   |-- controller/   SQLite state, reconciliation, and expiry
|   |   |-- previews/     detection, approval, runtime, secrets, and databases
|   |   |-- projects/     folder browsing and sandbox copies
|   |   `-- volumes/      Docker volume inspection and actions
|   |-- agent-images/     Claude Code and Codex Dockerfiles
|   `-- tests/            backend unit and Docker integration tests
|-- frontend/
|   |-- public/           static browser assets
|   `-- src/              React pages, components, hooks, and API clients
|-- docs/adr/             architecture decision records
`-- CONTEXT.md            domain language
```

## Local state and cleanup

The default SQLite database is `backend/.controller-data/controller.sqlite3`. This path is ignored by Git.

Sandboxes, credentials, preview data, and other runtime state can remain in Docker after the application stops.

Use the control panel to inspect and remove exact containers or volumes. Removing a volume permanently removes its stored data.

## Troubleshooting

### The backend reports that Docker is unavailable

Start Docker Engine or Docker Desktop. Confirm that the current user can access the Docker daemon.

### The folder picker rejects a path

Set `PROJECTS_ROOT` to an existing absolute parent folder. Select a child folder that does not escape through a symbolic link.

Restart the backend after you change environment variables.

### A coding agent does not start

Build the matching agent image. Confirm its tag matches `CLAUDE_AGENT_IMAGE` or `CODEX_AGENT_IMAGE`.

### A preview approval becomes invalid

A protected runtime file changed after inspection. Inspect the sandbox again and approve the new proposal revision.

### The frontend cannot reach the backend

Confirm that FastAPI listens on `127.0.0.1:8000`. Keep the default `/api` proxy or set `VITE_API_BASE` correctly.

### A source folder change does not appear

Sandbox copies do not synchronize. Create another sandbox to capture the current source-folder state.

## More documentation

- [`backend/README.md`](backend/README.md) documents backend behavior and endpoint details.
- [`frontend/README.md`](frontend/README.md) documents frontend development.
- [`backend/agent-images/README.md`](backend/agent-images/README.md) documents agent images and credential mounts.
- [`docs/adr/0001-controller-owned-preview-policy.md`](docs/adr/0001-controller-owned-preview-policy.md) explains controller-owned policy storage.

## License

This repository does not currently include a license file.
