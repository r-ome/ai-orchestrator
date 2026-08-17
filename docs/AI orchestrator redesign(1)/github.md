repo: r-ome/ai-orchestrator
branch: main
path: frontend

## Last sync
date: 2026-08-15T00:11:41Z

### Updated in this project
- Read new repo architecture: ProjectPage (repo-level), SandboxDetailPage, PlanningSessionPage with multi-tab workflow.
- Read DelegationWorkspace, SandboxPreviewSection, FeatureStatusBadge, planning + delegation APIs.
- Built 3 layout variations for the planning→preview workflow redesign (Planning Workflow.dc.html).

## Sync history
- 2026-08-05T07:54:10Z — Locked Graphite direction into control-plane shell with multi-screen nav and rebuild modal.
- 2026-08-05T07:23:30Z — initial 3-direction Project Detail redesign from frontend/src.

## Screen map
| Project screen | Repo source |
| --- | --- |
| Orchestrator.dc.html | frontend/src/pages/ProjectDetailPage.tsx (removed), components/ProjectAgentsSection.tsx (removed), ProjectPreviewSection.tsx (removed), AgentTerminal.tsx (removed) |
| Planning Workflow.dc.html | frontend/src/pages/PlanningSessionPage.tsx, SandboxDetailPage.tsx, ProjectPage.tsx, components/ProjectPlanningSection.tsx, SandboxPreviewSection.tsx, DelegationWorkspace.tsx, FeatureStatusBadge.tsx, api/planning.ts, api/delegation.ts, App.tsx |
