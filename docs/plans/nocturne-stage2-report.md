# Nocturne stage 2 report

## Files touched

- `frontend/src/App.css` — refined the Graphite rail, page headers,
  breadcrumbs, cards, tables, status pills, compact controls, meters, storage
  bars, tabs, dialogs, and the existing terminal surface. Primary actions now
  use the required accent outline and glow on a transparent ground.
- `frontend/src/App.tsx` — recorded the unavailable rail metrics instead of
  inventing them.
- `frontend/src/pages/SandboxDetailPage.tsx` — added the Graphite metric strip
  with existing sandbox fields, and documented the unavailable reference
  fields.
- `frontend/src/components/ContainerShell.tsx` — reads the stable terminal
  colours from the Nocturne variables instead of hard-coded colours.
- `docs/plans/nocturne-stage2-report.md` — this report.

Shared CSS restyles the listed Projects, Project, Containers, container detail,
Mounts, managed volumes, volume detail, storage, tab, collapsible card,
confirmation dialog, status badge, process table, volume table, storage bar,
and meter surfaces without changing their data or behaviour.

## Deliberately skipped mockup elements

- Right-hand terminal dock, collapsed dock rail, connection indicator, and
  dock controls. The app has no dock feature or layout state.
- Full-screen agent terminal. The request excludes this new feature. The
  existing container shell received the Graphite terminal treatment instead.
- Agent summon, attached-agent rows, stop, replace, and agent-terminal open
  actions. These change feature scope and API behaviour.
- The dedicated preview-stack rebuild modal and its approval and rebuild
  workflow. The app does not expose that workflow. Existing dialogs use the
  Graphite dialog surface only.
- Mockup-only preview actions such as “Inspect for rebuild” and “Open preview”.
  They would add behaviour beyond this visual stage.

## Mockup data the current APIs do not return

- The sandbox response has no copied file count, byte size, copy mode, or
  workspace mountpoint. It also has no live-agent count for the rail. The rail
  and sandbox detail carry `TODO(redesign)` comments and show only returned
  values.
- The project list response has no source-folder path, snapshot volume size,
  file count, or copy-progress state. The Projects table keeps its returned
  Git, branch, mirror, and sandbox data.
- The reference rebuild panel implies proposal revision, protected-runtime
  file changes, a selected rebuild set, and progress output. This stage does
  not add that workflow or fabricate those fields.

## Verification

- `cd frontend && npm run build` passed.
- `cd frontend && npm run lint` passed. Oxlint reported one existing Fast
  Refresh warning in `src/components/DelegationWorkspace.tsx:717`.

## Stage 2b correction

The metric strip repeated three fields already shown elsewhere on the sandbox
page, and rendered `db_engine` differently from the Lifecycle grid. Fixed by
moving `db_engine` and `feature_branch` out of the grid and into the strip,
carrying the "none — no database" vs "Not confirmed" distinction and its
comment with them, and dropping the duplicated Sandbox ID card. The strip now
carries three facts that appear nowhere else on the page.

Verified after the correction: `npm run build` passes (166ms); `npm run lint`
passes with only the pre-existing Fast Refresh warning at
`DelegationWorkspace.tsx:717`.
