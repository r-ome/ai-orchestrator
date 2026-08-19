# Orchestrator

Orchestrator is a local control plane for AI-assisted software work. It creates isolated Docker sandboxes, runs Claude Code or Codex, reviews changes, and controls application previews.

## Features

- Registers Git repositories and creates one feature sandbox per Docker volume.
- Runs clarifier, planner, and reviewer sessions before implementation.
- Splits approved plans into dependency-aware work items and verifies each result.
- Reviews the complete feature diff before publishing a branch or pull request.
- Runs approved native, Dockerfile, or Docker Compose previews.
- Manages preview secrets and sandbox MySQL, PostgreSQL, or SQLite databases.
- Inspects Docker containers, processes, terminals, mounts, volumes, and storage.

## Architecture

```text
React + Vite (:5173)
        |
        | HTTP and WebSocket through /api
        v
FastAPI (:8000) ---- SQLite workflow state
        |
        +---- Docker sandboxes, agents, previews, and databases
        +---- Git remotes
        `---- Claude and Codex providers
```

Docker volumes and Git hold code state. Controller-owned SQLite holds lifecycle, approval, and audit state.

## Requirements

- Docker Engine or Docker Desktop
- Python 3.11 or later
- [`uv`](https://docs.astral.sh/uv/)
- Node.js `^20.19.0`, `^22.13.0`, or `>=23.5.0`
- npm

## Run locally

Start the backend:

```bash
cd backend
uv sync --extra test
uv run uvicorn app.main:app --reload
```

Start the frontend in another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>. FastAPI documentation is at <http://127.0.0.1:8000/docs>.

### Build agent images

Planning and coding turns require at least one agent image:

```bash
cd backend
docker build -f agent-images/claude/Dockerfile -t orchestrator-agent-claude:latest agent-images/claude
docker build -f agent-images/codex/Dockerfile -t orchestrator-agent-codex:latest agent-images/codex
```

Sign in to each provider through an agent terminal. Credentials stay in provider-specific Docker volumes.

## Workflow

1. Register a Git repository.
2. Create a feature sandbox and confirm its database engine.
3. Start a planning session and approve the reviewed plan.
4. Run the delegation and review its verification evidence.
5. Inspect the feature diff and application preview.
6. Publish the reviewed branch or pull request.

Preview detection never runs project code automatically. A person must approve the exact proposal first.

## Configuration

Common environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTROLLER_DATA_DIRECTORY` | `backend/.controller-data` | Stores controller SQLite state. |
| `CONTROLLER_GIT_SECRET_DIRECTORY` | `~/.orchestrator/run-secrets` | Stores temporary Git credential files. |
| `ORCHESTRATOR_GITHUB_READ_TOKEN` | unset | Reads private GitHub repositories. |
| `ORCHESTRATOR_GITHUB_WRITE_TOKEN` | unset | Publishes GitHub branches and pull requests. |
| `CLAUDE_AGENT_IMAGE` | `orchestrator-agent-claude:latest` | Selects the Claude image. |
| `CODEX_AGENT_IMAGE` | `orchestrator-agent-codex:latest` | Selects the Codex image. |
| `VITE_API_BASE` | `/api` | Sets the frontend API base URL. |

Backend configuration also supports model, timeout, memory, preview, and routing overrides in the matching `backend/app/*/config.py` modules.

## Verify

```bash
cd backend
uv run pytest

cd ../frontend
npm run lint
npm test
npm run build
```

Docker integration tests require a running Docker daemon and their documented opt-in environment variables.

## Repository layout

```text
backend/app/          FastAPI controller
backend/tests/        backend tests
backend/agent-images/ Claude and Codex images
frontend/src/         React control panel
docs/adr/             architecture decisions
docs/plans/           design and implementation plans
CONTEXT.md            domain language
```

See [`backend/README.md`](backend/README.md) for backend details and [`backend/agent-images/README.md`](backend/agent-images/README.md) for agent image details.
