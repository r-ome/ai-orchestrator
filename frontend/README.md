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
