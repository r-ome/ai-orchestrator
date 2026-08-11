import { useCallback, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  confirmSandboxEngine,
  fetchSandbox,
  fetchSandboxEngine,
  removeSandbox,
  type EngineDetection,
} from '../api/projects'
import ConfirmDialog from '../components/ConfirmDialog'
import { useApiResource } from '../hooks/useApiResource'

const ENGINES = ['mysql', 'postgres', 'sqlite'] as const

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
  const [actor, setActor] = useState('operator')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [removeOpen, setRemoveOpen] = useState(false)

  const confirmEngine = async (event: FormEvent) => {
    event.preventDefault()
    if (!data || busy) return
    const detection: EngineDetection | null = engine.data
    setBusy(true)
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
      setBusy(false)
    }
  }

  const confirmRemove = async () => {
    if (!data) return
    setBusy(true)
    setActionError(null)
    try {
      await removeSandbox(data.sandbox_id)
      navigate('/projects', { replace: true })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
      setBusy(false)
    }
  }

  return (
    <section>
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to="/projects">Projects</Link>
            <span className="breadcrumb-separator" aria-hidden="true">/</span>
            <span className="breadcrumb-current" aria-current="page">
              {data?.feature_title || data?.feature_key || sandboxId}
            </span>
          </p>
          <h1>{data?.feature_title || data?.feature_key || 'Sandbox'}</h1>
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
            <span className="pill">{data.lifecycle_status ?? 'legacy'}</span>
            <span className="mono">{data.sandbox_id}</span>
          </div>
          <div className="card">
            <div className="card-header"><h2>Lifecycle</h2></div>
            <dl className="detail-grid">
              <dt>Feature key</dt><dd>{data.feature_key || '—'}</dd>
              <dt>Remote</dt><dd className="mono">{data.remote_url || '—'}</dd>
              <dt>Database engine</dt><dd>{data.db_engine || 'Not confirmed'}</dd>
              <dt>Feature branch</dt><dd className="mono">{data.feature_branch || '—'}</dd>
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
                    <select value={selectedEngine} onChange={(event) => setSelectedEngine(event.target.value as (typeof ENGINES)[number])} disabled={busy}>
                      {ENGINES.map((value) => <option key={value} value={value}>{value}</option>)}
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
        </>
      )}

      {removeOpen && data && (
        <ConfirmDialog
          title={`Remove ${data.feature_title || data.feature_key || data.sandbox_id}?`}
          confirmPhrase={`REMOVE ${data.feature_key || data.sandbox_id}`}
          confirmLabel="Remove sandbox"
          busy={busy}
          error={actionError}
          onCancel={() => setRemoveOpen(false)}
          onConfirm={confirmRemove}
        >
          <p>This removes only resources the sandbox manifest owns.</p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default SandboxDetailPage
