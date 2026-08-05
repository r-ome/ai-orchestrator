# Coding-agent images

These images contain agent software only. They contain no project files or
credentials.

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

The API overrides each image's default command with an idle process. It starts
the selected CLI in `tmux` after the first WebSocket connects. Later WebSockets
reattach to that session. Rebuild existing images after any Dockerfile change
so the container includes `tmux`.

The container root filesystem stays read-only. Only `/workspace`, `/auth`, and
the temporary `/tmp` filesystem are writable.

Set `CLAUDE_AGENT_IMAGE` or `CODEX_AGENT_IMAGE` to use different image tags.
