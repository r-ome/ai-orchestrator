# Coding-agent images

These images contain agent software and controller-owned browser verification
tools. They contain no project files or credentials.

Build both default images from the backend directory:

```bash
docker build \
  --file agent-images/claude/Dockerfile \
  --tag orchestrator-agent-claude:latest \
  agent-images/claude

docker build \
  --file agent-images/codex/Dockerfile \
  --tag orchestrator-agent-codex:latest \
  agent-images/codex
```

The builds need network access to Debian and npm package servers. Running an
agent needs network access to its provider. Provider subscription or API usage
can cause recurring external charges.

Both images pin Playwright and install its matching Chromium during the image
build. Coding turns use `/ms-playwright` read-only and expose the global package
through `NODE_PATH`. Agents must not download browsers or add browser packages
to a project during a turn.

Verify either built image without network access:

```bash
docker run --rm --network none --read-only --tmpfs /tmp \
  orchestrator-agent-claude:latest \
  node -e 'const {chromium}=require("playwright"); (async()=>{const b=await chromium.launch({headless:true}); const p=await b.newPage(); await p.setContent("<button>ok</button>"); if(await p.textContent("button")!=="ok") process.exitCode=1; await b.close()})()'
```

The API overrides each image's default command with an idle process. It starts
the selected CLI in `tmux` after the first WebSocket connects. Later WebSockets
reattach to that session. Rebuild existing images after any Dockerfile change
so new coding turns include the current CLI and browser tooling.

The container root filesystem stays read-only. Only `/workspace`, `/auth`, and
the temporary `/tmp` filesystem are writable.

Set `CLAUDE_AGENT_IMAGE` or `CODEX_AGENT_IMAGE` to use different image tags.
