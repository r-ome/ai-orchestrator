import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useParams,
} from 'react-router-dom'
import AgentTerminalPage from './pages/AgentTerminalPage'
import ContainersPage from './pages/ContainersPage'
import ContainerDetailPage from './pages/ContainerDetailPage'
import ProjectsPage from './pages/ProjectsPage'
import ProjectDetailPage from './pages/ProjectDetailPage'
import VolumesPage from './pages/VolumesPage'
import ManagedVolumesPage from './pages/ManagedVolumesPage'
import VolumeDetailPage from './pages/VolumeDetailPage'
import StorageStatusPage from './pages/StorageStatusPage'
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

function App() {
  return (
    <div className="page">
      <nav className="main-nav">
        {/* `end` on the links whose path is a prefix of a later nav item, so
            /volumes/managed lights up "Managed volumes" alone rather than
            "Mounts" as well. Links without it stay lit on their own detail
            pages: "Projects" on /projects/:name, "Managed volumes" on
            /volumes/managed/:name. "Containers" takes `end` so the container
            detail page does not also light it — its breadcrumb covers that. */}
        <NavLink to="/projects">Projects</NavLink>
        <NavLink to="/containers" end>
          Containers
        </NavLink>
        <NavLink to="/volumes" end>
          Mounts
        </NavLink>
        <NavLink to="/volumes/managed">Managed volumes</NavLink>
        <NavLink to="/volumes/status">Storage status</NavLink>
      </nav>

      <Routes>
        <Route path="/" element={<Navigate to="/projects" replace />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:projectName" element={<ProjectDetailPage />} />
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
        <Route path="*" element={<p className="status">Page not found.</p>} />
      </Routes>
    </div>
  )
}

export default App
