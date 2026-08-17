# Nocturne stage 4 report — Agent Roster (direction 1c)

Written by the reviewing agent. Codex could not write to `docs/` from its
sandbox, so this report records the verified outcome.

## Where the roster landed, and why not where the brief named

The brief pointed stage 4 at `DelegationWorkspace.tsx` and the sandbox
sections, hosted by `ProjectPage`. In this checkout those sandbox sections
(`SandboxPreviewSection`, `SandboxPublicationSection`, `SandboxStalenessSection`,
`SandboxRecoverySection`) are hosted by `SandboxDetailPage`, not `ProjectPage`.

More important: mockup 1c's roster shows the **planning** agents — clarifier,
planner, reviewer. Those live in `PlanningSessionPage`, driven by the planning
API's `*_provider` / `*_model` fields. So the agent-centric roster went into
`PlanningSessionPage.tsx`, which is where its real data is. The delegation and
sandbox section components received token-level restyling hooks only.

This means stage 4 superseded stage 3's contextual phase rail on the same
page. That is expected: stage 3 built the phase-centric "Planning Session"
mockup; the user chose the agent-centric direction 1c for the workflow. The
two are different information architectures for one screen, and 1c won. Stage
3's `planning-session-nav` rail was removed from both the TSX and the CSS — no
orphaned markup or dead styles remain.

## What the roster does

- A session bar, then a grid of agent cards (clarifier / planner / reviewer),
  then the selected agent's workspace panel below.
- Cards carry: icon tile, live status dot, role-based name, role, a monospace
  key/value stats block, and a footer status line.
- The selected card takes the accent border and glow.
- Phase navigation (`Tabs`) moved into the workspace header as **secondary**
  navigation, matching 1c's "phases are secondary" intent.
- Agent names stay role-based. Planning does not return agent names, so the
  mockup's fictional "Echo / Compass / Sentinel" were not used.

## Real data vs. mockup fiction

- Provider and model come from the planning API's real fields.
- The mockup's per-agent **cost** rows have no API source. Each is omitted with
  a `// TODO(redesign):` comment naming the missing field (clarifier, planner,
  reviewer cost). No cost or model values were fabricated — a repo-wide grep
  for cost literals and hard-coded model strings in the page returns nothing.

## Motion

- `noc-pulse` (live status dot) and `noc-spin` (footer spinner) are defined as
  keyframes and both disabled under `@media (prefers-reduced-motion: reduce)`.

## Verification (run by the reviewing agent, not self-reported)

- `cd frontend && npm run build` — passes, built in ~190ms.
- `cd frontend && npm run lint` — passes with only the pre-existing Fast
  Refresh warning at `DelegationWorkspace.tsx:717` (present before this work).
- Section-component diffs are additive className hooks only; no logic changed.
