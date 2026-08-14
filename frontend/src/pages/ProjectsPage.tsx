import { useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { fetchRemoteProjects, registerRemoteProject } from '../api/projects'
import { projectLabel } from '../api/sandboxes'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

/**
 * The projects list. A project is a Git repository; its sandboxes live on its
 * own page. This page deliberately shows no sandboxes, so "which project am I
 * in?" is answered before any sandbox is reachable.
 */
function ProjectsPage() {
  const { data, loading, error, reload } = useApiResource(fetchRemoteProjects)
  const [remoteUrl, setRemoteUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const addProjectCard = useRef<HTMLDivElement>(null)

  const projects = useMemo(
    () =>
      [...(data?.projects ?? [])].sort((a, b) =>
        projectLabel(a.remote_url).localeCompare(projectLabel(b.remote_url)),
      ),
    [data],
  )
  const canSubmit = remoteUrl.trim() !== '' && !busy

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    setBusy(true)
    setFormError(null)
    setNotice(null)
    try {
      const project = await registerRemoteProject(remoteUrl.trim())
      setNotice(
        `Project ${projectLabel(project.remote_url)} is registered. Open it to create a sandbox.`,
      )
      setRemoteUrl('')
      reload()
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
              addProjectCard.current?.scrollIntoView({
                behavior: 'smooth',
                block: 'start',
              })
            }
          >
            Add project
          </button>
          <button type="button" onClick={reload} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </header>

      <p className="status">
        A project is a Git repository. Open one to see its sandboxes, where a
        sandbox is one feature branch workspace copied from it.
      </p>

      <div className="card">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Projects</h2>
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
          {!error && !loading && projects.length === 0 && (
            <p className="status">No projects yet. Add one below.</p>
          )}
        </div>
        {!error && !loading && projects.length > 0 && (
          <div className="table-wrapper">
            <table className="chrome-table">
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Git remote</th>
                  <th className="numeric">Sandboxes</th>
                  <th>Default branch</th>
                  <th>Mirror fetched</th>
                </tr>
              </thead>
              <tbody>
                {projects.map((project) => (
                  <tr key={project.project_id}>
                    <td>
                      <Link
                        to={`/projects/${encodeURIComponent(project.project_id)}`}
                      >
                        {projectLabel(project.remote_url)}
                      </Link>
                    </td>
                    <td className="mono">{project.remote_url}</td>
                    <td className="numeric">{project.sandbox_count}</td>
                    <td className="mono">{project.default_branch || '—'}</td>
                    <td
                      title={
                        project.mirror_fetched_at
                          ? formatTimestamp(project.mirror_fetched_at)
                          : undefined
                      }
                    >
                      {project.mirror_fetched_at
                        ? formatRelativeTime(project.mirror_fetched_at)
                        : 'Never'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div ref={addProjectCard} className="card" id="add-project">
        <div className="card-header">
          <h2>Add a project</h2>
        </div>
        <div className="card-body">
          <p className="status">
            Registering a project records the remote. The shared mirror is
            fetched when its first sandbox is created.
          </p>
          <form className="file-form" onSubmit={submit}>
            <label>
              Git remote URL
              <input
                type="url"
                value={remoteUrl}
                onChange={(event) => setRemoteUrl(event.target.value)}
                placeholder="https://github.com/owner/repository.git"
                required
              />
            </label>
            <button type="submit" className="primary" disabled={!canSubmit}>
              {busy ? 'Adding…' : 'Add project'}
            </button>
          </form>
          {formError && (
            <p className="status status-error" role="alert">{formError}</p>
          )}
          {notice && <p className="status status-ok">{notice}</p>}
        </div>
      </div>

    </section>
  )
}

export default ProjectsPage
