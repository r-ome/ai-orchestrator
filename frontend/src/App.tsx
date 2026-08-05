import { lazy, Suspense, useCallback } from 'react'
import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
  useParams,
} from 'react-router-dom'
const AgentTerminalPage = lazy(() => import('./pages/AgentTerminalPage'))
const ContainersPage = lazy(() => import('./pages/ContainersPage'))
const ContainerDetailPage = lazy(() => import('./pages/ContainerDetailPage'))
const ProjectsPage = lazy(() => import('./pages/ProjectsPage'))
const ProjectDetailPage = lazy(() => import('./pages/ProjectDetailPage'))
const VolumesPage = lazy(() => import('./pages/VolumesPage'))
const ManagedVolumesPage = lazy(() => import('./pages/ManagedVolumesPage'))
const VolumeDetailPage = lazy(() => import('./pages/VolumeDetailPage'))
const StorageStatusPage = lazy(() => import('./pages/StorageStatusPage'))
const ProjectTerminalDock = lazy(() => import('./components/ProjectTerminalDock'))
import { fetchAgents } from './api/agents'
import { fetchProject } from './api/projects'
import { useApiResource } from './hooks/useApiResource'
import './App.css'

function LegacyContainerRedirect() {
  const { containerId } = useParams()
  return (
    <Navigate
      to={`/containers/detail/${encodeURIComponent(containerId ?? '')}`}
      replace
    />
  )
}

// A plain square, no icon library — the design calls for a border-only
// glyph per nav item, distinguished by a small internal detail.
function RailIcon({ variant }: { variant: 'square' | 'bars' | 'circle' }) {
  if (variant === 'bars') {
    return (
      <span className="rail-icon rail-icon-bars" aria-hidden="true">
        <span />
      </span>
    )
  }
  if (variant === 'circle') {
    return <span className="rail-icon rail-icon-circle" aria-hidden="true" />
  }
  return <span className="rail-icon rail-icon-square" aria-hidden="true" />
}

// Loads the current sandbox summary for project detail and agent-terminal
// routes. The project endpoint supplies file and size counts; the agent list
// supplies the live-agent count shown in the reference rail.
function CurrentProjectSummary() {
  const location = useLocation()
  const projectMatch = location.pathname.match(/^\/projects\/([^/]+)/)
  const projectName = projectMatch ? decodeURIComponent(projectMatch[1]) : ''
  const projectFetcher = useCallback(
    (signal: AbortSignal) =>
      projectName ? fetchProject(projectName, signal) : Promise.resolve(null),
    [projectName],
  )
  const agentFetcher = useCallback(
    (signal: AbortSignal) =>
      projectName
        ? fetchAgents(signal)
        : Promise.resolve({ count: 0, agents: [] }),
    [projectName],
  )
  const project = useApiResource(projectFetcher, [projectName])
  const agents = useApiResource(agentFetcher, [projectName])
  if (!projectName) return null

  const liveAgents =
    agents.data?.agents.filter(
      (agent) =>
        agent.project_name === projectName &&
        ['created', 'running', 'restarting', 'paused'].includes(agent.status),
    ).length ?? 0

  return (
    <div className="rail-current">
      <div className="rail-current-label">Current sandbox</div>
      <div className="rail-current-name">
        {project.data?.name ?? projectName}
      </div>
      <div className="rail-current-stats">
        <div>
          <span>files</span>
          <strong>{project.data?.file_count ?? '—'}</strong>
        </div>
        <div>
          <span>size</span>
          <strong>{project.data?.copied_size ?? '—'}</strong>
        </div>
        <div>
          <span>agents</span>
          <strong className={liveAgents > 0 ? 'live' : ''}>
            {liveAgents} live
          </strong>
        </div>
      </div>
    </div>
  )
}

function App() {
  const location = useLocation()
  const projectMatch = location.pathname.match(/^\/projects\/([^/]+)/)
  const currentSandboxPath = projectMatch
    ? `/projects/${projectMatch[1]}`
    : '/projects'
  const hasCurrentSandbox = Boolean(projectMatch)
  const isTerminal = location.pathname.includes('/agents/')
  const isProjectDetail =
    hasCurrentSandbox &&
    !isTerminal &&
    location.pathname.split('/').filter(Boolean).length === 2
  const projectName = projectMatch ? decodeURIComponent(projectMatch[1]) : ''

  return (
    <div className="app-shell">
      <nav className="app-rail">
        <div className="app-rail-brand">
          <div className="app-rail-mark" aria-hidden="true">
            O
          </div>
          <div className="app-rail-wordmark">Orchestrator</div>
        </div>

        <div className="app-rail-nav">
          <NavLink to="/projects" end>
            <RailIcon variant="square" />
            Projects
          </NavLink>
          <NavLink
            to={currentSandboxPath}
            className={hasCurrentSandbox ? undefined : () => ''}
          >
            <RailIcon variant="bars" />
            Sandboxes
          </NavLink>
          <NavLink to="/containers" end>
            <RailIcon variant="square" />
            Containers
          </NavLink>
          <NavLink to="/volumes" end>
            <RailIcon variant="bars" />
            Mounts
          </NavLink>
          <NavLink to="/volumes/status">
            <RailIcon variant="circle" />
            Storage status
          </NavLink>
        </div>

        <CurrentProjectSummary />
      </nav>

      <main className="app-main">
        <div className={`page${isTerminal ? ' page-terminal' : ''}`}>
          <Suspense fallback={<p className="status">Loading page…</p>}>
            <Routes>
            <Route path="/" element={<Navigate to="/projects" replace />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route
              path="/projects/:projectName"
              element={<ProjectDetailPage />}
            />
            <Route
              path="/projects/:projectName/agents/:agentId"
              element={<AgentTerminalPage />}
            />
            <Route path="/containers" element={<ContainersPage />} />
            {/* Old split-tab URLs, kept so bookmarks still land somewhere. */}
            <Route
              path="/containers/all"
              element={<Navigate to="/containers" replace />}
            />
            <Route
              path="/containers/all/:containerId"
              element={<LegacyContainerRedirect />}
            />
            <Route
              path="/containers/detail/:containerId"
              element={<ContainerDetailPage />}
            />
            <Route
              path="/containers/status"
              element={<Navigate to="/containers" replace />}
            />
            <Route path="/volumes" element={<VolumesPage />} />
            <Route path="/volumes/managed" element={<ManagedVolumesPage />} />
            <Route
              path="/volumes/managed/:volumeName"
              element={<VolumeDetailPage />}
            />
            <Route path="/volumes/status" element={<StorageStatusPage />} />
            <Route
              path="*"
              element={<p className="status">Page not found.</p>}
            />
            </Routes>
          </Suspense>
        </div>
      </main>
      {isProjectDetail && (
        <Suspense fallback={null}>
          <ProjectTerminalDock projectName={projectName} />
        </Suspense>
      )}
    </div>
  )
}

export default App
