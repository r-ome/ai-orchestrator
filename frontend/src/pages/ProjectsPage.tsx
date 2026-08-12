import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  fetchCopyJobs,
  fetchProjects,
} from '../api/projects'
import {
  createSandbox,
  fetchSandboxes,
  groupByProject,
  projectLabel,
  sandboxLabel,
} from '../api/sandboxes'
import CopyJobsTable from '../components/CopyJobsTable'
import CopyLogModal from '../components/CopyLogModal'
import CopyStatusBadge from '../components/CopyStatusBadge'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

function ProjectsPage() {
  const { data, loading, error, reload } = useApiResource(fetchProjects)
  const jobs = useApiResource(fetchCopyJobs)
  const sandboxes = useApiResource(fetchSandboxes)

  const [remoteUrl, setRemoteUrl] = useState('')
  const [featureKey, setFeatureKey] = useState('')
  const [featureTitle, setFeatureTitle] = useState('')
  const [logJobId, setLogJobId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const createSandboxCard = useRef<HTMLDivElement>(null)

  const projects = useMemo(
    () => groupByProject(sandboxes.data?.sandboxes ?? []),
    [sandboxes.data],
  )
  const knownRemotes = useMemo(
    () =>
      projects.filter(
        (project): project is typeof project & { remoteUrl: string } =>
          Boolean(project.remoteUrl),
      ),
    [projects],
  )
  // An empty choice means "a repository not listed here", which reveals the
  // URL field. Picking an existing project is the common case, so a sandbox
  // never has to be created from a URL typed from memory.
  const [remoteChoice, setRemoteChoice] = useState('')
  const chosenRemote = remoteChoice || remoteUrl
  // Preselect a project the first time the list arrives, so the form opens on
  // the common case instead of on an empty URL field.
  const remoteChoiceInitialized = useRef(false)
  useEffect(() => {
    if (remoteChoiceInitialized.current || knownRemotes.length === 0) return
    remoteChoiceInitialized.current = true
    setRemoteChoice(knownRemotes[0].remoteUrl)
  }, [knownRemotes])

  const canSubmit = chosenRemote.trim() !== '' && featureKey.trim() !== '' && !busy

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
      const sandbox = await createSandbox({
        remote_url: chosenRemote.trim(),
        feature_key: featureKey.trim(),
        feature_title: featureTitle.trim() || undefined,
      })
      // Name the project as well as the sandbox. The sandbox ID alone never
      // says which repository the new sandbox belongs to.
      setNotice(
        `Sandbox "${sandboxLabel(sandbox)}" in ${projectLabel(sandbox.remote_url)}` +
          ` is ${sandbox.lifecycle_status ?? 'creating'}.`,
      )
      setFeatureKey('')
      setFeatureTitle('')
      sandboxes.reload()
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
        A project is a Git repository. A sandbox is one feature branch
        workspace copied from it. The feature key is a stable, human-supplied
        identifier for that branch.
      </p>

      <div ref={createSandboxCard} className="card" id="create-sandbox">
        <div className="card-header">
          <h2>Create a sandbox</h2>
        </div>
        <div className="card-body">
          <form className="file-form" onSubmit={submit}>
            {knownRemotes.length > 0 && (
              <label>
                Project
                <select
                  value={remoteChoice}
                  onChange={(event) => setRemoteChoice(event.target.value)}
                >
                  {knownRemotes.map((project) => (
                    <option key={project.projectId} value={project.remoteUrl}>
                      {project.label}
                    </option>
                  ))}
                  <option value="">Another repository…</option>
                </select>
              </label>
            )}
            {remoteChoice === '' && (
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
            )}
            <label>
              Feature key
              <input
                type="text"
                value={featureKey}
                onChange={(event) => setFeatureKey(event.target.value)}
                placeholder="add-login"
                pattern="[a-z0-9][a-z0-9-]{0,63}"
                required
              />
            </label>
            <label>
              Feature title
              <input
                type="text"
                value={featureTitle}
                onChange={(event) => setFeatureTitle(event.target.value)}
                placeholder="Add login"
              />
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
            <h2>Sandboxes by project</h2>
            {sandboxes.data && <span className="pill">{sandboxes.data.count}</span>}
          </div>
          <button type="button" onClick={sandboxes.reload} disabled={sandboxes.loading}>
            {sandboxes.loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
        <div className="card-body">
          {sandboxes.error && (
            <p className="status status-error" role="alert">
              Failed to load sandboxes: {sandboxes.error}
            </p>
          )}
          {!sandboxes.error && sandboxes.loading && <p className="status">Loading sandboxes…</p>}
          {!sandboxes.error && !sandboxes.loading && projects.length === 0 && (
            <p className="status">No projects yet.</p>
          )}
        </div>
        {/* One table per project rather than a project column, so which
            repository a sandbox belongs to survives scrolling. */}
        {!sandboxes.error && !sandboxes.loading && projects.map((project) => (
          <div key={project.projectId} className="table-wrapper">
            <h3 className="table-caption">
              {project.label}
              <span className="pill">{project.sandboxes.length}</span>
            </h3>
            {project.remoteUrl && (
              <p className="status mono">{project.remoteUrl}</p>
            )}
            <table className="chrome-table">
              <thead>
                <tr>
                  <th>Sandbox</th>
                  <th>Lifecycle status</th>
                  <th>Feature branch</th>
                  <th>Sandbox ID</th>
                </tr>
              </thead>
              <tbody>
                {project.sandboxes.map((sandbox) => (
                  <tr key={sandbox.sandbox_id}>
                    <td>
                      <Link to={`/sandboxes/${encodeURIComponent(sandbox.sandbox_id)}`}>
                        {sandboxLabel(sandbox)}
                      </Link>
                    </td>
                    <td>{sandbox.lifecycle_status ?? 'legacy'}</td>
                    <td className="mono">{sandbox.feature_branch || '—'}</td>
                    <td className="mono">{sandbox.sandbox_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Legacy local copies</h2>
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
            <p className="status">No legacy local copies.</p>
          )}
          {!error && !loading && data && data.projects.length > 0 && (
            <p className="status">
              Folders copied from this machine, from before sandboxes were
              created from a Git remote. They are not projects.
            </p>
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

    </section>
  )
}

export default ProjectsPage
