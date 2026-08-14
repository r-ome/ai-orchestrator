import { lazy, Suspense, useCallback } from 'react'
import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
  useParams,
} from 'react-router-dom'
const PlanningSessionPage = lazy(() => import('./pages/PlanningSessionPage'))
const ContainersPage = lazy(() => import('./pages/ContainersPage'))
const ContainerDetailPage = lazy(() => import('./pages/ContainerDetailPage'))
const ProjectsPage = lazy(() => import('./pages/ProjectsPage'))
const ProjectPage = lazy(() => import('./pages/ProjectPage'))
const SandboxDetailPage = lazy(() => import('./pages/SandboxDetailPage'))
const VolumesPage = lazy(() => import('./pages/VolumesPage'))
const ManagedVolumesPage = lazy(() => import('./pages/ManagedVolumesPage'))
const VolumeDetailPage = lazy(() => import('./pages/VolumeDetailPage'))
const StorageStatusPage = lazy(() => import('./pages/StorageStatusPage'))
import { fetchRemoteProject } from './api/projects'
import { fetchSandbox, projectLabel, sandboxLabel } from './api/sandboxes'
import { useApiResource } from './hooks/useApiResource'
import { useTheme } from './hooks/useTheme'
import type { ThemeChoice } from './theme'
import './App.css'

const THEME_OPTIONS: { value: ThemeChoice; label: string; title: string }[] = [
  { value: 'light', label: 'Light', title: 'Always use the light theme' },
  { value: 'dark', label: 'Dark', title: 'Always use the dark theme' },
  { value: 'system', label: 'Auto', title: 'Follow the system appearance' },
]

// Sits at the foot of the rail. A segmented control rather than a single
// toggle, so "follow the system" stays reachable as its own state instead of
// being the hidden default you can never get back to.
function ThemeControl() {
  const { choice, resolved, setChoice } = useTheme()
  return (
    <div className="rail-theme">
      <div className="rail-theme-label" id="rail-theme-label">
        Appearance
      </div>
      <div
        className="rail-theme-options"
        role="radiogroup"
        aria-labelledby="rail-theme-label"
      >
        {THEME_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={choice === option.value}
            className={choice === option.value ? 'is-active' : undefined}
            title={
              option.value === 'system'
                ? `${option.title} (currently ${resolved})`
                : option.title
            }
            onClick={() => setChoice(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  )
}

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

// Loads the current sandbox summary for managed sandbox routes. Names both
// the sandbox and the project it was copied from, because the sandbox ID
// alone never says which repository you are working in.
function CurrentSandboxSummary({ sandboxId }: { sandboxId: string }) {
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchSandbox(sandboxId, signal),
    [sandboxId],
  )
  const sandbox = useApiResource(fetcher, [sandboxId])
  return (
    <div className="rail-current">
      <div className="rail-current-label">Current project</div>
      <div className="rail-current-name">
        {sandbox.data ? projectLabel(sandbox.data.remote_url) : '—'}
      </div>
      <div className="rail-current-label">Sandbox</div>
      <div className="rail-current-name">
        {sandbox.data ? sandboxLabel(sandbox.data) : sandboxId}
      </div>
      <div className="rail-current-stats">
        <div>
          <span>status</span>
          <strong>{sandbox.data?.lifecycle_status ?? '—'}</strong>
        </div>
        <div>
          <span>engine</span>
          <strong>{sandbox.data?.db_engine ?? 'unconfirmed'}</strong>
        </div>
      </div>
    </div>
  )
}

// Names the project on its own page. The page header says it too, but the
// rail is the one thing that stays put while the page scrolls.
function CurrentProjectRailSummary({ projectId }: { projectId: string }) {
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchRemoteProject(projectId, signal),
    [projectId],
  )
  const project = useApiResource(fetcher, [projectId])
  return (
    <div className="rail-current">
      <div className="rail-current-label">Current project</div>
      <div className="rail-current-name">
        {project.data ? projectLabel(project.data.remote_url) : '—'}
      </div>
      <div className="rail-current-stats">
        <div>
          <span>sandboxes</span>
          <strong>{project.data?.sandbox_count ?? '—'}</strong>
        </div>
        <div>
          <span>branch</span>
          <strong>{project.data?.default_branch || '—'}</strong>
        </div>
      </div>
    </div>
  )
}

function App() {
  const location = useLocation()
  // Projects are Git repositories. Their sandboxes have their own routes.
  const projectMatch = location.pathname.match(/^\/projects\/([^/]+)/)
  const sandboxMatch = location.pathname.match(/^\/sandboxes\/([^/]+)/)
  // The Sandboxes link points at the closest thing to a sandbox you are in.
  const currentSandboxPath = sandboxMatch
    ? `/sandboxes/${sandboxMatch[1]}`
    : projectMatch
      ? `/projects/${projectMatch[1]}`
      : '/projects'
  const hasCurrentSandbox = Boolean(sandboxMatch ?? projectMatch)

  return (
    <div className="app-shell">
      <nav className="app-rail">
        <div className="app-rail-brand">
          <div className="app-rail-mark" aria-hidden="true">
            O
          </div>
          <div className="app-rail-wordmark">Orchestrator</div>
        </div>

        {sandboxMatch ? (
          <CurrentSandboxSummary
            sandboxId={decodeURIComponent(sandboxMatch[1])}
          />
        ) : projectMatch ? (
          <CurrentProjectRailSummary
            projectId={decodeURIComponent(projectMatch[1])}
          />
        ) : null}

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

        {/* This wrapper's `margin-top: auto` pins the Appearance control to
            the bottom of the rail, flush with the window edge. */}
        <div className="app-rail-foot">
          <ThemeControl />
        </div>
      </nav>

      <main className="app-main">
        <div className="page">
          <Suspense fallback={<p className="status">Loading page…</p>}>
            <Routes>
            <Route path="/" element={<Navigate to="/projects" replace />} />
            <Route path="/projects" element={<ProjectsPage />} />
            {/* A project is a Git repository, keyed by its opaque project ID.
                Its sandboxes live on its page, not on the list. */}
            <Route path="/projects/:projectId" element={<ProjectPage />} />
            <Route path="/sandboxes/:sandboxId" element={<SandboxDetailPage />} />
            <Route
              path="/sandboxes/:sandboxId/plans/:sessionId"
              element={<PlanningSessionPage />}
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
    </div>
  )
}

export default App
