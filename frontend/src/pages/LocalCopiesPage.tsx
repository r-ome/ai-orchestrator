import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchCopyJobs, fetchProjects } from '../api/projects'
import CopyJobsTable from '../components/CopyJobsTable'
import CopyLogModal from '../components/CopyLogModal'
import CopyStatusBadge from '../components/CopyStatusBadge'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

/**
 * Folders copied from this machine, from before sandboxes were created from a
 * Git remote. They have a source path and no remote, so they are not projects
 * and never appear in the projects list.
 */
function LocalCopiesPage() {
  const { data, loading, error, reload } = useApiResource(fetchProjects)
  const jobs = useApiResource(fetchCopyJobs)
  const [logJobId, setLogJobId] = useState<string | null>(null)

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

  return (
    <section>
      <header className="page-header">
        <h1>Local copies</h1>
        <div className="button-row">
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
        Folders copied from this machine, from before sandboxes were created
        from a Git remote. They are not projects. See{' '}
        <Link to="/projects">Projects</Link> for repositories and their
        sandboxes.
      </p>

      <div className="card">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Local copies</h2>
            {data && <span className="pill">{data.count}</span>}
          </div>
        </div>
        <div className="card-body">
          {error && (
            <p className="status status-error" role="alert">
              Failed to load local copies: {error}
            </p>
          )}
          {!error && loading && <p className="status">Loading local copies…</p>}
          {!error && !loading && data && data.projects.length === 0 && (
            <p className="status">No local copies.</p>
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
                      <Link to={`/local/${encodeURIComponent(project.name)}`}>
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

          {!jobs.error && jobs.loading && <p className="status">Loading jobs…</p>}

          {!jobs.error && !jobs.loading && jobs.data && (
            <>
              {jobs.data.jobs.length === 0 ? (
                <p className="status">No copy jobs yet.</p>
              ) : (
                <>
                  {copyIsActive && (
                    <p className="status">Refreshing every second.</p>
                  )}
                  <CopyJobsTable jobs={jobs.data.jobs} onShowLog={setLogJobId} />
                  <p className="status">
                    Job status and logs are stored inside each copy's volume,
                    at
                    <span className="mono"> .orchestrator/copy-job</span>. They
                    survive removing the helper container, and disappear only
                    when the volume does.
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
    </section>
  )
}

export default LocalCopiesPage
