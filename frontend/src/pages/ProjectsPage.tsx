import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  createProjectSandbox,
  fetchCopyJobs,
  fetchProjects,
} from '../api/projects'
import CopyJobsTable from '../components/CopyJobsTable'
import CopyLogModal from '../components/CopyLogModal'
import CopyStatusBadge from '../components/CopyStatusBadge'
import FolderPicker from '../components/FolderPicker'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

function ProjectsPage() {
  const { data, loading, error, reload } = useApiResource(fetchProjects)
  const jobs = useApiResource(fetchCopyJobs)

  const [path, setPath] = useState('')
  const [picking, setPicking] = useState(false)
  const [logJobId, setLogJobId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const createSandboxCard = useRef<HTMLDivElement>(null)

  const pathValid = path.startsWith('/')
  const canSubmit = pathValid && !busy

  const reloadJobs = jobs.reload
  const copyIsActive =
    data?.projects.some(
      (project) =>
        project.copy_status === 'queued' || project.copy_status === 'copying',
    ) ||
    jobs.data?.jobs.some(
      (job) => job.status === 'queued' || job.status === 'copying',
    )

  // Poll both lists while any copy is still running.
  useEffect(() => {
    if (!copyIsActive) return

    const timer = window.setInterval(() => {
      reload()
      reloadJobs()
    }, 1_000)
    return () => window.clearInterval(timer)
  }, [copyIsActive, reload, reloadJobs])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    setBusy(true)
    setFormError(null)
    setNotice(null)
    try {
      const job = await createProjectSandbox(path.trim())
      setNotice(
        `Started ${job.project_name}. Copy status: ${job.status}. Job: ${job.job_id}.`,
      )
      reload()
      reloadJobs()
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <header className="page-header">
        <h1>Projects</h1>
        <div className="button-row">
          <button
            type="button"
            className="primary"
            onClick={() =>
              createSandboxCard.current?.scrollIntoView({
                behavior: 'smooth',
                block: 'start',
              })
            }
          >
            Create sandbox
          </button>
          <button
            type="button"
            onClick={() => {
              reload()
              reloadJobs()
            }}
            disabled={loading}
          >
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </header>

      <p className="status">
        Each sandbox is a one-time snapshot of a host folder, copied into a
        dedicated Docker volume. Names use the folder name with an
        incrementing suffix, such as
        <span className="mono"> my-project-sandbox-1</span>. Later edits to
        the source folder do not sync.
      </p>

      <div ref={createSandboxCard} className="card" id="create-sandbox">
        <div className="card-header">
          <h2>Create a project sandbox</h2>
        </div>
        <div className="card-body">
          <form className="file-form" onSubmit={submit}>
            <label>
              Project folder
              <div className="picker-field">
                <span className="mono">{path || 'No folder chosen'}</span>
                <button type="button" onClick={() => setPicking(true)}>
                  Browse…
                </button>
              </div>
            </label>

            <button type="submit" className="primary" disabled={!canSubmit}>
              {busy ? 'Starting…' : 'Create sandbox'}
            </button>
          </form>

          {formError && (
            <p className="status status-error" role="alert">
              {formError}
            </p>
          )}

          {notice && <p className="status status-ok">{notice}</p>}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Project sandboxes</h2>
            {data && <span className="pill">{data.count}</span>}
          </div>
        </div>
        <div className="card-body">
          {error && (
            <p className="status status-error" role="alert">
              Failed to load projects: {error}
            </p>
          )}

          {!error && loading && <p className="status">Loading projects…</p>}

          {!error && !loading && data && data.projects.length === 0 && (
            <p className="status">No project sandboxes yet.</p>
          )}
        </div>

        {!error && !loading && data && data.projects.length > 0 && (
          <div className="table-wrapper">
            <table className="chrome-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Source folder</th>
                  <th>Volume</th>
                  <th>Copy status</th>
                  <th className="numeric">Files</th>
                  <th className="numeric">Size</th>
                  <th>Registered</th>
                </tr>
              </thead>
              <tbody>
                {data.projects.map((project) => (
                  <tr key={project.volume_name}>
                    <td>
                      <Link
                        to={`/projects/${encodeURIComponent(project.name)}`}
                      >
                        {project.name}
                      </Link>
                    </td>
                    <td className="mono">{project.source_path}</td>
                    <td className="mono">
                      <Link
                        to={`/volumes/managed/${encodeURIComponent(project.volume_name)}`}
                      >
                        {project.volume_name}
                      </Link>
                    </td>
                    <td>
                      <CopyStatusBadge status={project.copy_status} />
                    </td>
                    <td className="numeric">{project.file_count}</td>
                    <td className="numeric mono">{project.copied_size}</td>
                    <td title={formatTimestamp(project.created_at)}>
                      {formatRelativeTime(project.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Copy jobs</h2>
            {jobs.data && <span className="pill">{jobs.data.count}</span>}
          </div>
        </div>
        <div className="card-body">
          {jobs.error && (
            <p className="status status-error" role="alert">
              Failed to load copy jobs: {jobs.error}
            </p>
          )}

          {!jobs.error && jobs.loading && (
            <p className="status">Loading jobs…</p>
          )}

          {!jobs.error && !jobs.loading && jobs.data && (
            <>
              {jobs.data.jobs.length === 0 ? (
                <p className="status">No copy jobs yet.</p>
              ) : (
                <>
                  {copyIsActive && (
                    <p className="status">Refreshing every second.</p>
                  )}
                  <CopyJobsTable
                    jobs={jobs.data.jobs}
                    onShowLog={setLogJobId}
                  />
                  <p className="status">
                    Job status and logs are stored inside each project
                    volume, at
                    <span className="mono"> .orchestrator/copy-job</span>.
                    They survive removing the helper container, and
                    disappear only when the volume does.
                  </p>
                </>
              )}
            </>
          )}
        </div>
      </div>

      {/* Rendered outside the list block so a background refresh cannot
          unmount it mid-read. */}
      {logJobId && (
        <CopyLogModal jobId={logJobId} onClose={() => setLogJobId(null)} />
      )}

      {picking && (
        <FolderPicker
          onCancel={() => setPicking(false)}
          onSelect={(chosen) => {
            setPath(chosen)
            setPicking(false)
          }}
        />
      )}
    </section>
  )
}

export default ProjectsPage
