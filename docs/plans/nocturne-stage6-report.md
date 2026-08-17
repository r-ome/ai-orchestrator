# Nocturne Stage 6 report

## Files touched

- `frontend/src/pages/PlanningSessionPage.tsx`
- `frontend/src/App.css`
- `docs/plans/nocturne-stage6-report.md`

## Inspector and selection state

The page now conditionally renders a 340px right inspector only when
`selectedAgentId` has a selected sidebar agent. A phase click sets both
`chosenTab` and that phase's first agent ID. An agent-row click sets only the
agent ID. The inspector close button clears the ID, so the center column grows
into the released space.

Nested agent rows are real buttons. They have selected styling and use the
shared focus-visible accent ring. The active status dot uses a token-based
animation that stops when reduced motion is requested.

## Inspector data

Provider and model come from the session's clarifier, planner, and reviewer
fields. The work-item agent uses the existing delegation run or routing data.
Reviewer controls also show the real `reviewer_reasoning_effort` value.

Turns equal the count of `session.messages` whose `role` equals the selected
agent role. Work-item turns are zero because planning messages do not carry a
work-item role. History uses those same filtered messages, ordered by sequence.
It infers labels from `questions`, `revision`, and `approved`, and shows real
approved or confirmed badges.

The Raw output tab fetches the latest matching message with `has_raw_output`
from the existing raw-output endpoint. It shows a muted empty state when none
exists.

The only unavailable metrics remain `cost`, `tokens in`, and `tokens out`.
They render as `—` under one `TODO(redesign)` comment because the planning API
does not expose usage. Per-agent overrides remain read-only because no planning
API action supports changing them after a session starts.

## Breadcrumb

The first crumb now uses `data.project_name`, with the sandbox project label as
its fallback, and links to the project. The second crumb uses `data.title`,
then the sandbox feature title or feature key. It never falls back to the
opaque sandbox ID.

## Verification

- `cd frontend && npm run build` — passed.
- `cd frontend && npm run lint` — passed with the accepted existing Fast
  Refresh warning at `DelegationWorkspace.tsx:717`.
