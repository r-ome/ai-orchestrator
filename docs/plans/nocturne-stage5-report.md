# Nocturne Stage 5 report

## Files touched

- `frontend/src/App.tsx`
- `frontend/src/App.css`
- `frontend/src/pages/PlanningSessionPage.tsx`
- `docs/plans/nocturne-stage5-report.md`

## Layout changes

`App.tsx` checks `location.pathname` against the planning-session route pattern.
It omits the global `.app-rail` only for `/sandboxes/:sandboxId/plans/:sessionId`.
The planning page renders its own 250px sidebar and center column on that route.

The sidebar reuses the shared brand markup and links the project name to its
project page. It fetches the real planning-session list with
`fetchPlanningSessions(projectName)`. If that request fails, or omits the
current session, it renders only the current session.

The roster grid, roster selection state, `AgentRosterCard`, `RosterIcon`, and
their CSS are removed. The sidebar phase navigation now selects the existing
page phase state. The existing phase bodies and confirmation/cancellation
actions remain in place.

## Session status mapping

- `feature_status === building` → `building · {review_turn}/{max_review_turns}`
- `under_review` → `under review · rev {plan_revision}`
- `plan_ready` → `plan ready`
- `planning` → `planning · rev {plan_revision}`
- `review_limit_reached` → `review limit reached`
- `awaiting_confirmation` → `awaiting confirmation`
- All other states use the API status value.

## API limits

The planning session does not provide a session-level work-item provider or
model before a work-item run exists. The page shows routing when the existing
delegation data provides it. The source has a `TODO(redesign)` note for the
missing pre-run field.

## Verification

- `cd frontend && npm run build` — passed.
- `cd frontend && npm run lint` — passed with the accepted existing Fast
  Refresh warning at `DelegationWorkspace.tsx:717`.
