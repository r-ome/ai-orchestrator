import { useCallback, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  fetchRemoteProject,
  removeRemoteProject,
  type RemoteProject,
} from '../api/projects'
import {
  createSandbox,
  fetchSandboxes,
  projectLabel,
  removeSandbox,
  sandboxLabel,
  type Sandbox,
} from '../api/sandboxes'
import ConfirmDialog from '../components/ConfirmDialog'
import ProjectSecretsSection from '../components/ProjectSecretsSection'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

function ProjectPage() {
  const { projectId = '' } = useParams()
  const navigate = useNavigate()

  const projectFetcher = useCallback(
    (signal: AbortSignal) => fetchRemoteProject(projectId, signal),
    [projectId],
  )
  const project = useApiResource(projectFetcher, [projectId])
  // The sandbox list is not project-scoped on the wire, so it is filtered
  // here. One request keeps the page consistent with the projects list.
  const sandboxes = useApiResource(fetchSandboxes)
  const mine = useMemo(
    () =>
      (sandboxes.data?.sandboxes ?? [])
        .filter((sandbox) => sandbox.project_id === projectId)
        .sort((a, b) => sandboxLabel(a).localeCompare(sandboxLabel(b))),
    [sandboxes.data, projectId],
  )

  const secretsSandbox = useMemo(
    () => mine.find((sandbox) => sandbox.lifecycle_status === 'ready') ?? null,
    [mine],
  )

  const [featureKey, setFeatureKey] = useState('')
  const [featureTitle, setFeatureTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)
  const [removeError, setRemoveError] = useState<string | null>(null)
  const [removeStep, setRemoveStep] = useState<string | null>(null)

  const label = project.data
    ? projectLabel(project.data.remote_url)
    : projectId
  const canSubmit = featureKey.trim() !== '' && !busy

  const reloadAll = () => {
    project.reload()
    sandboxes.reload()
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit || !project.data) return

    setBusy(true)
    setFormError(null)
    setNotice(null)
    try {
      // The project is already fixed by the page, so the form never asks
      // which repository this sandbox belongs to.
      const sandbox = await createSandbox({
        remote_url: project.data.remote_url,
        feature_key: featureKey.trim(),
        feature_title: featureTitle.trim() || undefined,
      })
      setNotice(
        `Sandbox "${sandboxLabel(sandbox)}" is ${sandbox.lifecycle_status ?? 'creating'}.`,
      )
      setFeatureKey('')
      setFeatureTitle('')
      reloadAll()
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy(false)
    }
  }

  // Removal destroys each sandbox through DELETE /sandboxes/{id}, the only
  // path that takes the lifecycle lease and drains writers, and only then
  // removes the project. The project route refuses while any sandbox remains.
  const confirmRemove = async () => {
    if (!project.data || busy) return
    setBusy(true)
    setRemoveError(null)
    try {
      for (const sandbox of mine) {
        setRemoveStep(`Removing sandbox ${sandboxLabel(sandbox)}…`)
        await removeSandbox(sandbox.sandbox_id)
      }
      setRemoveStep('Removing the project…')
      await removeRemoteProject(projectId)
      navigate('/projects', { replace: true })
    } catch (err) {
      setRemoveError(err instanceof Error ? err.message : 'Unknown error')
      setRemoveStep(null)
      setBusy(false)
      reloadAll()
    }
  }

  return (
    <section className="project-page">
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to="/projects">Projects</Link>
            <span className="breadcrumb-separator" aria-hidden="true">/</span>
            <span className="breadcrumb-current" aria-current="page">{label}</span>
          </p>
          <h1>{label}</h1>
          {project.data && (
            <p className="page-subtitle mono">{project.data.remote_url}</p>
          )}
        </div>
        <div className="button-row">
          <button
            type="button"
            onClick={reloadAll}
            disabled={project.loading || sandboxes.loading || busy}
          >
            {project.loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </header>

      {project.error && (
        <p className="status status-error" role="alert">
          Failed to load project: {project.error}
        </p>
      )}
      {!project.error && project.loading && (
        <p className="status">Loading project…</p>
      )}

      {project.data && (
        <>
          <SandboxesCard
            sandboxes={mine}
            loading={sandboxes.loading}
            error={sandboxes.error}
          />

          <div className="card">
            <div className="card-header"><h2>Create a sandbox</h2></div>
            <div className="card-body">
              <p className="status">
                A sandbox is one feature branch workspace copied from{' '}
                <strong>{label}</strong>. The feature key is a stable,
                human-supplied identifier for that branch.
              </p>
              <form className="file-form" onSubmit={submit}>
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
                <p className="status status-error" role="alert">{formError}</p>
              )}
              {notice && <p className="status status-ok">{notice}</p>}
            </div>
          </div>

          <MirrorCard project={project.data} />

          {/* Secrets are stored per project, but the only route that reads
              them resolves a sandbox. So they are addressed through a ready
              sandbox of this project; every sandbox here shares one set. */}
          {secretsSandbox ? (
            <ProjectSecretsSection
              projectName={secretsSandbox.sandbox_id}
              projectReady
            />
          ) : (
            <div className="card">
              <div className="card-header"><h2>Secrets</h2></div>
              <div className="card-body">
                <p className="status">
                  Secrets belong to the project and are shared by all its
                  sandboxes, but they are only reachable through a ready
                  sandbox. Create one to set them.
                </p>
              </div>
            </div>
          )}

          <div className="card">
            <div className="card-header"><h2>Danger zone</h2></div>
            <div className="card-body">
              <p className="status">
                Removing this project destroys every sandbox in it, each
                sandbox database, and the shared mirror. This cannot be undone.
              </p>
              {removeError && (
                <p className="status status-error" role="alert">{removeError}</p>
              )}
              <button
                type="button"
                className="danger"
                onClick={() => {
                  setRemoveError(null)
                  setRemoveOpen(true)
                }}
                disabled={busy}
              >
                Remove project
              </button>
            </div>
          </div>
        </>
      )}

      {removeOpen && project.data && (
        <ConfirmDialog
          title={`Remove ${label}?`}
          confirmPhrase={`REMOVE ${label}`}
          confirmLabel="Remove project"
          busy={busy}
          error={removeError}
          onCancel={() => {
            if (busy) return
            setRemoveOpen(false)
          }}
          onConfirm={confirmRemove}
        >
          <p>
            This destroys {mine.length} sandbox
            {mine.length === 1 ? '' : 'es'} and the shared mirror for{' '}
            <strong>{label}</strong>.
          </p>
          {mine.length > 0 && (
            <ul>
              {mine.map((sandbox) => (
                <li key={sandbox.sandbox_id}>{sandboxLabel(sandbox)}</li>
              ))}
            </ul>
          )}
          <p>
            Each sandbox is removed one at a time, so live work is drained
            before its resources go. Nothing on the Git remote is touched.
          </p>
          {removeStep && <p className="status">{removeStep}</p>}
        </ConfirmDialog>
      )}
    </section>
  )
}

function SandboxesCard({
  sandboxes,
  loading,
  error,
}: {
  sandboxes: Sandbox[]
  loading: boolean
  error: string | null
}) {
  return (
    <div className="card">
      <div className="card-header">
        <div className="card-header-title">
          <h2>Sandboxes</h2>
          {!error && !loading && <span className="pill">{sandboxes.length}</span>}
        </div>
      </div>
      <div className="card-body">
        {error && (
          <p className="status status-error" role="alert">
            Failed to load sandboxes: {error}
          </p>
        )}
        {!error && loading && <p className="status">Loading sandboxes…</p>}
        {!error && !loading && sandboxes.length === 0 && (
          <p className="status">
            No sandboxes in this project yet. Create one below.
          </p>
        )}
      </div>
      {!error && !loading && sandboxes.length > 0 && (
        <div className="table-wrapper">
          <table className="chrome-table">
            <thead>
              <tr>
                <th>Sandbox</th>
                <th>Lifecycle status</th>
                <th>Database engine</th>
                <th>Feature branch</th>
              </tr>
            </thead>
            <tbody>
              {sandboxes.map((sandbox) => (
                <tr key={sandbox.sandbox_id}>
                  <td>
                    <Link to={`/sandboxes/${encodeURIComponent(sandbox.sandbox_id)}`}>
                      {sandboxLabel(sandbox)}
                    </Link>
                  </td>
                  <td>{sandbox.lifecycle_status ?? 'creating'}</td>
                  <td>
                    {sandbox.db_engine === 'none'
                      ? 'none'
                      : sandbox.db_engine || 'Not confirmed'}
                  </td>
                  <td className="mono">{sandbox.feature_branch || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function MirrorCard({ project }: { project: RemoteProject }) {
  return (
    <div className="card">
      <div className="card-header"><h2>Canonical mirror</h2></div>
      <div className="card-body">
        {!project.mirror_fetched_at && (
          <p className="status">
            Not fetched yet. Creating the first sandbox fetches the mirror
            under the project mirror lock.
          </p>
        )}
        <dl className="detail-grid">
          <dt>Volume</dt>
          <dd className="mono">
            {project.mirror_volume ? (
              <Link to={`/volumes/managed/${encodeURIComponent(project.mirror_volume)}`}>
                {project.mirror_volume}
              </Link>
            ) : (
              '—'
            )}
          </dd>
          <dt>Default branch</dt>
          <dd className="mono">{project.default_branch || '—'}</dd>
          <dt>Last fetched</dt>
          <dd title={project.mirror_fetched_at ? formatTimestamp(project.mirror_fetched_at) : undefined}>
            {project.mirror_fetched_at
              ? formatRelativeTime(project.mirror_fetched_at)
              : 'Never'}
          </dd>
        </dl>
      </div>
    </div>
  )
}

export default ProjectPage
