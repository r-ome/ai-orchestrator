import { useCallback, useState } from 'react'
import {
  fetchSandboxStaleness,
  type Sandbox,
  type SyncSandboxResult,
} from '../api/sandboxes'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime } from '../utils/format'

interface SandboxStalenessSectionProps {
  sandbox: Sandbox
  /** Any lifecycle action is running; every control disables. */
  busy: boolean
  /** This section's own action is running; only it shows progress. */
  pending: boolean
  onSync: (stopBlockingPreview: boolean) => Promise<SyncSandboxResult | null>
}

function SandboxStalenessSection({
  sandbox,
  busy,
  pending,
  onSync,
}: SandboxStalenessSectionProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchSandboxStaleness(sandbox.sandbox_id, signal),
    [sandbox.sandbox_id],
  )
  const staleness = useApiResource(fetcher, [fetcher])
  const [stopBlockingPreview, setStopBlockingPreview] = useState(false)
  const [syncResult, setSyncResult] = useState<SyncSandboxResult | null>(null)

  const sync = async () => {
    const result = await onSync(stopBlockingPreview)
    if (!result) return
    setSyncResult(result)
    staleness.reload()
  }

  return (
    <div className="card">
      <div className="card-header"><h2>Base staleness</h2></div>
      <div className="card-body">
        {staleness.loading && <p className="status">Loading staleness…</p>}
        {staleness.error && <p className="status status-error" role="alert">Failed to load staleness: {staleness.error}</p>}
        {staleness.data && (
          <>
            <dl className="detail-grid">
              <dt>Base status</dt>
              <dd>
                {staleness.data.behind_count === null
                  ? 'unknown'
                  : staleness.data.behind_count === 0
                    ? `up to date with ${staleness.data.base_ref}`
                    : `${staleness.data.behind_count} commits behind ${staleness.data.base_ref}`}
              </dd>
              <dt>Current base commit</dt>
              <dd>
                <span className="mono" title={staleness.data.current_base_commit}>
                  {staleness.data.current_base_commit.slice(0, 12)}
                </span>
              </dd>
              <dt>Mirror fetched</dt>
              <dd>{formatRelativeTime(staleness.data.mirror_fetched_at ?? '')}</dd>
            </dl>
            {staleness.data.stale_answer && (
              <p className="status status-error" role="alert">
                Mirror refresh failed: {staleness.data.fetch_failure_reason || 'reason unavailable'}; this answer may be out of date.
              </p>
            )}
          </>
        )}
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={stopBlockingPreview}
            onChange={(event) => setStopBlockingPreview(event.target.checked)}
            disabled={busy}
          />
          Stop a running preview before syncing
        </label>
        <div className="button-row">
          <button type="button" className="primary" onClick={sync} disabled={busy}>
            {pending ? 'Syncing…' : 'Sync base'}
          </button>
        </div>
        {syncResult && (
          <dl className="detail-grid">
            <dt>Strategy</dt><dd>{syncResult.strategy}</dd>
            <dt>Safety ref</dt><dd className="mono">{syncResult.safety_ref}</dd>
            <dt>Operation</dt><dd className="mono">{syncResult.operation_id}</dd>
          </dl>
        )}
        {syncResult?.engine_report.mismatch && (
          <p className="status status-error" role="alert">
            The confirmed engine is {syncResult.engine_report.confirmed_engine || 'unknown'}, but the workspace now detects {syncResult.engine_report.detected_engine || 'unknown'}. The sandbox is still usable but its database no longer matches the code. Reset database to recover.
          </p>
        )}
        {syncResult?.engine_report.detection_error && (
          <p className="status status-error" role="alert">
            Engine detection: {syncResult.engine_report.detection_error}
          </p>
        )}
      </div>
    </div>
  )
}

export default SandboxStalenessSection
