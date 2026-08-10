# Frontend

The React and Vite frontend manages sandboxes, coding agents, preview proposals,
Docker containers, and volumes.

## Run

```bash
npm install
npm run dev
```

Vite proxies `/api` and WebSocket traffic to `http://127.0.0.1:8000`.

## Verify

```bash
npm run build
```

The project detail page enforces explicit review. It shows detected preview
settings and protected-file differences before enabling start or rebuild.

The Feature review tab shows the accepted commit range, file totals, and a
bounded unified diff. An approved review enables a confirmed, fast-forward-only
merge into the original project folder. The same tab previews the full
implementation and accepts small change instructions for an agent. Each
successful change reruns full verification, invalidates the prior review, and
stays awaiting review. The history shows missing acceptance evidence. An
approved whole-feature review completes every held change for that commit.
