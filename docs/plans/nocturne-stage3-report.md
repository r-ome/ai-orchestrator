# Nocturne stage 3 report

## Navigation decision

The planning session uses a contextual phase rail inside the existing app
shell. The global app rail stays the only permanent sidebar. The contextual
rail contains the current session, its breadcrumb, and phase navigation. It
does not load or invent other sessions because this page already has only the
current session response. Routing is unchanged.

## Files touched

- `frontend/src/App.css` — added the planning-session layout, responsive
  contextual rail, phase progress treatment, human and agent conversation
  thread, round cards, plan-spec sections, work-item status bar, and planning
  console/raw-output treatment. Markdown headings now use Nocturne's 500
  heading weight.
- `frontend/src/pages/PlanningSessionPage.tsx` — moved the existing phase tabs
  into the contextual rail, added the current session summary, real planning
  provider/model labels, header refresh control, and the differentiated
  clarification thread layout.
- `frontend/src/components/Tabs.tsx` — accepts optional tab metadata while
  retaining its existing ARIA tab and keyboard behaviour.
- `frontend/src/components/PlanningTurnCard.tsx` — adds role and recorded
  model metadata to planner and reviewer turns.
- `frontend/src/components/PlanningStatusBadge.tsx` — adds planning status
  classes for the active-turn treatment.
- `frontend/src/components/PlanningRawOutput.tsx` — adds the planning-specific
  raw-output hook class.
- `frontend/src/components/PlanSpecView.tsx` — groups scope, approach,
  components, risks, and questions into the plan-spec layout.
- `frontend/src/components/PlanDiff.tsx` — adds the planning-specific diff
  hook class.
- `frontend/src/components/TurnConsole.tsx` — adds the planning-specific live
  console hook class.
- `frontend/src/components/DelegationWorkspace.tsx` — shows the returned
  completed-item count in the work-items status bar.
- `docs/plans/nocturne-stage3-report.md` — this report.

## API data and mockup substitutions

The phase rail uses the returned `clarifier_provider`, `planner_provider`,
`reviewer_provider`, and their recorded model fields. It does not use the
mockup's fictional agent names.

The mockup shows several sessions. This screen has one session response and
does not fetch the session list. The rail therefore shows only the current
session. This avoids a new request and preserves existing behaviour.

## Verification

- `cd frontend && npm run build` passed.
- `cd frontend && npm run lint` passed with one existing Fast Refresh warning:
  `src/components/DelegationWorkspace.tsx:717`.
