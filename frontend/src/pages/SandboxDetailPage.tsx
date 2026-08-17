import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  confirmSandboxEngine,
  fetchSandbox,
  fetchSandboxEngine,
  projectLabel,
  publishSandbox,
  removeSandbox,
  resetSandboxDatabase,
  resumeSandbox,
  sandboxLabel,
  syncSandbox,
  type EngineDetection,
  type PublishSandboxResult,
  type SyncSandboxResult,
} from '../api/sandboxes'
import ConfirmDialog from '../components/ConfirmDialog'
import ProjectPlanningSection from '../components/ProjectPlanningSection'
import SandboxPreviewSection from '../components/SandboxPreviewSection'
import SandboxPublicationSection from '../components/SandboxPublicationSection'
import SandboxRecoverySection from '../components/SandboxRecoverySection'
import SandboxStalenessSection from '../components/SandboxStalenessSection'
import { useApiResource } from '../hooks/useApiResource'

const ENGINES = ['mysql', 'postgres', 'sqlite', 'none'] as const
const ENGINE_LABELS: Record<(typeof ENGINES)[number], string> = {
  mysql: 'mysql',
  postgres: 'postgres',
  sqlite: 'sqlite',
  none: 'none — this project has no database',
}

export type SandboxAction =
  | 'confirm-engine'
  | 'sync'
  | 'publish'
  | 'reset-db'
  | 'resume'
  | 'remove'

function SandboxDetailPage() {
  const { sandboxId = '' } = useParams()
  const navigate = useNavigate()
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchSandbox(sandboxId, signal),
    [sandboxId],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [sandboxId])
  const engineFetcher = useCallback(
    (signal: AbortSignal) =>
      data?.lifecycle_status === 'awaiting_engine_confirmation'
        ? fetchSandboxEngine(sandboxId, signal)
        : Promise.resolve(null),
    [data?.lifecycle_status, sandboxId],
  )
  const engine = useApiResource(engineFetcher, [engineFetcher])
  const [selectedEngine, setSelectedEngine] = useState<(typeof ENGINES)[number]>('sqlite')
  const selectedEngineInitialized = useRef(false)
  useEffect(() => {
    selectedEngineInitialized.current = false
    setSelectedEngine('sqlite')
  }, [sandboxId])
  useEffect(() => {
    if (!engine.data || selectedEngineInitialized.current) return
    selectedEngineInitialized.current = true
    setSelectedEngine(engine.data.proposed_engine ?? 'sqlite')
  }, [engine.data])
  const [actor, setActor] = useState('operator')
  // The backend holds one lease per sandbox, so only one lifecycle operation may
  // be in flight. Naming it also keeps each button's progress label truthful.
  const [busyAction, setBusyAction] = useState<SandboxAction | null>(null)
  const busy = busyAction !== null
  const setBusy = (running: boolean, action: SandboxAction) =>
    setBusyAction(running ? action : null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)

  const confirmEngine = async (event: FormEvent) => {
    event.preventDefault()
    if (!data || busy) return
    const detection: EngineDetection | null = engine.data
    setBusy(true, 'confirm-engine')
    setActionError(null)
    try {
      await confirmSandboxEngine(data.sandbox_id, {
        engine: selectedEngine,
        migrate_commands: detection?.migrate_commands ?? [],
        seed_commands: detection?.seed_commands ?? [],
        commands_source: detection?.commands_source ?? {},
        actor: actor.trim() || 'operator',
      })
      await reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy(false, 'confirm-engine')
    }
  }

  const confirmRemove = async () => {
    if (!data || busy) return
    setBusy(true, 'remove')
    setActionError(null)
    try {
      const projectId = data.project_id
      await removeSandbox(data.sandbox_id)
      // Back to the project, not the list: the other sandboxes of this
      // project are the likely next stop.
      navigate(`/projects/${encodeURIComponent(projectId)}`, { replace: true })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
      setBusy(false, 'remove')
    }
  }

  const sync = async (stopBlockingPreview: boolean): Promise<SyncSandboxResult | null> => {
    if (!data || busy) return null
    setBusy(true, 'sync')
    setActionError(null)
    try {
      const result = await syncSandbox(data.sandbox_id, {
        stop_blocking_preview: stopBlockingPreview,
      })
      reload()
      return result
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
      return null
    } finally {
      setBusy(false, 'sync')
    }
  }

  const publish = async (
    stopBlockingPreview: boolean,
  ): Promise<PublishSandboxResult | null> => {
    if (!data || busy) return null
    setBusy(true, 'publish')
    setActionError(null)
    try {
      const result = await publishSandbox(data.sandbox_id, {
        stop_blocking_preview: stopBlockingPreview,
      })
      reload()
      return result
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
      return null
    } finally {
      setBusy(false, 'publish')
    }
  }

  const resetDatabase = async (stopBlockingPreview: boolean): Promise<boolean> => {
    if (!data || busy) return false
    setBusy(true, 'reset-db')
    setActionError(null)
    try {
      await resetSandboxDatabase(data.sandbox_id, {
        stop_blocking_preview: stopBlockingPreview,
      })
      reload()
      return true
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
      return false
    } finally {
      setBusy(false, 'reset-db')
    }
  }

  const resume = async (): Promise<boolean> => {
    if (!data || busy) return false
    setBusy(true, 'resume')
    setActionError(null)
    try {
      await resumeSandbox(data.sandbox_id)
      reload()
      return true
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
      return false
    } finally {
      setBusy(false, 'resume')
    }
  }

  return (
    <section>
      <header className="page-header">
        <div>
          {/* Three levels, because a sandbox is meaningless without the
              project it was copied from. */}
          <p className="breadcrumb">
            <Link to="/projects">Projects</Link>
            <span className="breadcrumb-separator" aria-hidden="true">/</span>
            {data && (
              <>
                <Link to={`/projects/${encodeURIComponent(data.project_id)}`}>
                  {projectLabel(data.remote_url)}
                </Link>
                <span className="breadcrumb-separator" aria-hidden="true">/</span>
              </>
            )}
            <span className="breadcrumb-current" aria-current="page">
              {data ? sandboxLabel(data) : sandboxId}
            </span>
          </p>
          <h1>{data ? sandboxLabel(data) : 'Sandbox'}</h1>
          {data && (
            <p className="page-subtitle">
              Sandbox of <strong>{projectLabel(data.remote_url)}</strong>
            </p>
          )}
        </div>
        <div className="button-row">
          <button type="button" onClick={reload} disabled={loading || busy}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
          <button type="button" className="danger" onClick={() => setRemoveOpen(true)} disabled={!data || busy}>
            Remove sandbox
          </button>
        </div>
      </header>

      {error && <p className="status status-error" role="alert">Failed to load sandbox: {error}</p>}
      {!error && loading && <p className="status">Loading sandbox…</p>}
      {actionError && <p className="status status-error" role="alert">{actionError}</p>}

      {data && (
        <>
          <div className="detail-status-row">
            <span className="pill">{data.lifecycle_status ?? 'creating'}</span>
            <span className="mono">{data.sandbox_id}</span>
          </div>
          {/* TODO(redesign): the sandbox API does not expose copied file count,
              byte size, copy mode, or workspace mountpoint from the reference. */}
          <div className="metric-strip sandbox-metric-strip">
            <div className="card metric-card">
              <div className="section-heading">Data volume</div>
              <div className="metric-value">{data.db_data_volume || '—'}</div>
            </div>
            <div className="card metric-card">
              <div className="section-heading">Database</div>
              {/* A bare "none" would read as "not confirmed yet". These are
                  different states, and only the latter needs operator action. */}
              <div className="metric-value">
                {data.db_engine === 'none'
                  ? 'none — no database'
                  : data.db_engine || 'Not confirmed'}
              </div>
            </div>
            <div className="card metric-card">
              <div className="section-heading">Feature branch</div>
              <div className="metric-value">{data.feature_branch || '—'}</div>
            </div>
          </div>
          <div className="card">
            <div className="card-header"><h2>Lifecycle</h2></div>
            <dl className="detail-grid">
              <dt>Project</dt><dd>{projectLabel(data.remote_url)}</dd>
              <dt>Feature key</dt><dd>{data.feature_key || '—'}</dd>
              <dt>Remote</dt><dd className="mono">{data.remote_url || '—'}</dd>
            </dl>
          </div>

          {data.lifecycle_status === 'awaiting_engine_confirmation' && (
            <div className="card">
              <div className="card-header"><h2>Confirm database engine</h2></div>
              <div className="card-body">
                <p className="status">This sandbox cannot become ready until an operator confirms its database engine.</p>
                {engine.data?.proposed_engine && (
                  <p className="status">Detected engine: <span className="mono">{engine.data.proposed_engine}</span></p>
                )}
                {engine.error && <p className="status status-error" role="alert">Failed to load detection: {engine.error}</p>}
                <form className="file-form" onSubmit={confirmEngine}>
                  <label>
                    Database engine
                    <select
                      value={selectedEngine}
                      onChange={(event) => {
                        selectedEngineInitialized.current = true
                        setSelectedEngine(event.target.value as (typeof ENGINES)[number])
                      }}
                      disabled={busy}
                    >
                      {ENGINES.map((value) => <option key={value} value={value}>{ENGINE_LABELS[value]}</option>)}
                    </select>
                  </label>
                  <label>
                    Confirmed by
                    <input value={actor} onChange={(event) => setActor(event.target.value)} disabled={busy} required />
                  </label>
                  <button type="submit" className="primary" disabled={busy || engine.loading}>
                    {busy ? 'Confirming…' : 'Confirm engine'}
                  </button>
                </form>
              </div>
            </div>
          )}

          {data.lifecycle_status === 'ready' && (
            // Planning routes take a sandbox ID in the `:projectName`
            // position. That is what the v1 branch of
            // `inspect_registered_project` expects. Do not "fix" it.
            <ProjectPlanningSection
              projectName={data.sandbox_id}
              projectReady={data.lifecycle_status === 'ready'}
              subject="sandbox"
              basePath={`/sandboxes/${encodeURIComponent(data.sandbox_id)}`}
            />
          )}

          {/* Above sync and publish, because a running preview is what makes
              those two ask for consent. */}
          {data.lifecycle_status === 'ready' && (
            <SandboxPreviewSection sandboxId={data.sandbox_id} busy={busy} />
          )}

          {data.lifecycle_status === 'ready' && (
            <SandboxStalenessSection
              sandbox={data}
              busy={busy}
              pending={busyAction === 'sync'}
              onSync={sync}
            />
          )}

          {data.lifecycle_status === 'ready' && (
            <SandboxPublicationSection
              sandbox={data}
              busy={busy}
              pending={busyAction === 'publish'}
              actionError={actionError}
              onPublish={publish}
            />
          )}

          {['ready', 'database_failed', 'degraded'].includes(data.lifecycle_status ?? '') && (
            <SandboxRecoverySection
              sandbox={data}
              busy={busy}
              resetPending={busyAction === 'reset-db'}
              resumePending={busyAction === 'resume'}
              actionError={actionError}
              onResetDatabase={resetDatabase}
              onResume={resume}
            />
          )}
        </>
      )}

      {removeOpen && data && (
        <ConfirmDialog
          title={`Remove ${sandboxLabel(data)}?`}
          confirmPhrase={`REMOVE ${data.feature_key || data.sandbox_id}`}
          confirmLabel="Remove sandbox"
          busy={busyAction === 'remove'}
          error={actionError}
          onCancel={() => setRemoveOpen(false)}
          onConfirm={confirmRemove}
        >
          {/* Name the project too. Two sandboxes of different projects can
              share a feature key, and this dialog is not reversible. */}
          <p>
            Sandbox <strong>{sandboxLabel(data)}</strong> of project{' '}
            <strong>{projectLabel(data.remote_url)}</strong>.
          </p>
          <p>This removes only resources the sandbox manifest owns. The sandbox database and its data are destroyed with it.</p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default SandboxDetailPage
