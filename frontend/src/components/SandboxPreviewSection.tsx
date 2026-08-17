import { Fragment, useCallback, useEffect, useState } from 'react'
import {
  fetchCurrentPreview,
  fetchPreviewLogs,
  keepPreviewAlive,
  inspectPreview,
  startPreview,
  stopPreview,
  type PreviewLogs,
  type PreviewProposal,
  type PreviewRun,
} from '../api/previews'
import { describeSharing } from '../utils/databaseSharing'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

/** The backend's answer when this sandbox is not running a preview. */
const NO_PREVIEW = 'Sandbox has no active preview'
/** Match the delegation workspace, which extends by the same amount. */
const KEEP_ALIVE_MINUTES = 30

interface SandboxPreviewSectionProps {
  /** Preview routes take a sandbox ID in the `:project_name` position. */
  sandboxId: string
  /** Any lifecycle action is running; every control disables. */
  busy: boolean
}

/**
 * Shows the preview a sandbox is running, and lets an operator start or stop it.
 *
 * Previews are *built* from the delegation workspace, where the feature being
 * previewed is in view. This section starts only proposals that the controller
 * can run safely without extra review or environment input.
 */
function SandboxPreviewSection({ sandboxId, busy }: SandboxPreviewSectionProps) {
  const [current, setCurrent] = useState<PreviewRun | null>(null)
  const [logs, setLogs] = useState<PreviewLogs | null>(null)
  const [confirmStop, setConfirmStop] = useState(false)
  const [removeData, setRemoveData] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [blockedProposal, setBlockedProposal] = useState<PreviewProposal | null>(null)
  const [clock, setClock] = useState(Date.now())

  const loadCurrent = useCallback(async () => {
    try {
      setCurrent(await fetchCurrentPreview(sandboxId))
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error'
      // "No preview" is an answer, not a failure. Anything else is real.
      if (message === NO_PREVIEW) {
        setCurrent(null)
        setError(null)
      } else {
        setError(message)
      }
    }
  }, [sandboxId])

  useEffect(() => {
    setLogs(null)
    setConfirmStop(false)
    setNotice(null)
    // A refusal belongs to one sandbox's proposal. Carrying it to the next
    // sandbox would blame it for protected files it never changed.
    setBlockedProposal(null)
    void loadCurrent()
  }, [loadCurrent])

  // Only tick while an expiry is on screen, so an idle sandbox page renders once.
  useEffect(() => {
    if (!current?.expires_at) return
    const timer = window.setInterval(() => setClock(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [current?.expires_at])

  const run = async (operation: () => Promise<void>) => {
    setLoading(true)
    setError(null)
    setNotice(null)
    try {
      await operation()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const keepAlive = () =>
    run(async () => {
      setCurrent(await keepPreviewAlive(sandboxId, KEEP_ALIVE_MINUTES))
      setNotice(`Preview extended by ${KEEP_ALIVE_MINUTES} minutes.`)
    })

  const showLogs = () =>
    run(async () => {
      setLogs(await fetchPreviewLogs(sandboxId))
    })

  const stop = () =>
    run(async () => {
      const result = await stopPreview(sandboxId, removeData)
      setConfirmStop(false)
      setCurrent(null)
      setLogs(null)
      setNotice(
        `Preview stopped. Removed ${result.removed_containers} containers, ` +
          `${result.removed_networks} networks, ${result.removed_volumes} volumes.`,
      )
    })

  const viewProject = () =>
    run(async () => {
      const proposal = await inspectPreview(sandboxId)
      if (proposal.approval_required || proposal.missing_environment.length > 0) {
        setBlockedProposal(proposal)
        return
      }
      setCurrent(await startPreview(sandboxId, proposal, proposal.config, 'start', false))
      setBlockedProposal(null)
    })

  const remainingSeconds = current?.expires_at
    ? Math.max(Math.ceil((Date.parse(current.expires_at) - clock) / 1_000), 0)
    : null
  const expiryWarning = remainingSeconds !== null && remainingSeconds <= 300
  const disabled = busy || loading

  return (
    <div className="card sandbox-preview-section">
      <div className="card-header">
        <h2>Preview</h2>
        <button type="button" onClick={() => void loadCurrent()} disabled={disabled}>
          Refresh
        </button>
      </div>
      <div className="card-body">
        {error && <p className="status status-error" role="alert">{error}</p>}
        {notice && <p className="status status-ok">{notice}</p>}

        {!current && !error && (
          <>
            <p className="status">
              No preview is running. View project builds one from the sandbox's working
              tree using the configuration the controller detects. To preview one task's
              commit, or to change that configuration, use the delegation workspace of a
              plan instead.
            </p>
            <div className="button-row">
              <button
                type="button"
                className="primary"
                onClick={() => void viewProject()}
                disabled={disabled}
              >
                {/* Building an image can take minutes. A static label here reads
                    as a dead button. */}
                {loading ? 'Starting preview…' : 'View project'}
              </button>
            </div>
            {blockedProposal && (
              <div className="status status-warning">
                {blockedProposal.approval_required && (
                  <p>
                    {blockedProposal.changes.length > 0
                      ? `Protected files changed: ${blockedProposal.changes.length}. Human review is required.`
                      : 'This sandbox has no approved preview configuration yet. Human review is required.'}
                  </p>
                )}
                {blockedProposal.missing_environment.length > 0 && (
                  <p>
                    Missing environment variables:{' '}
                    {blockedProposal.missing_environment.map((name, index) => (
                      <Fragment key={name}>
                        {index > 0 && ', '}<span className="mono">{name}</span>
                      </Fragment>
                    ))}
                  </p>
                )}
                <p>
                  Review it in the plan's delegation workspace from the Planning section on this page.
                </p>
                <button type="button" onClick={() => setBlockedProposal(null)} disabled={disabled}>
                  Dismiss
                </button>
              </div>
            )}
          </>
        )}

        {current && (
          <>
            <dl className="detail-grid">
              <dt>Status</dt>
              <dd>{current.status}</dd>
              <dt>Preview</dt>
              <dd className="mono" title={current.id}>{current.id.slice(0, 12)}</dd>
              {/* A task preview is pinned to one verified commit; a live one
                  follows the working tree. The difference decides what the
                  operator is actually looking at. */}
              <dt>Built from</dt>
              <dd>
                {current.kind === 'task'
                  ? `task ${(current.task_id ?? '').slice(0, 12) || 'unknown'}`
                  : 'the working tree'}
                {current.commit_sha ? ` at ${current.commit_sha.slice(0, 12)}` : ''}
              </dd>
              <dt>Service</dt>
              <dd>{current.selected_service || 'app'}</dd>
              <dt>Port</dt>
              <dd className="mono">
                127.0.0.1:{current.host_port} → {current.container_port}
              </dd>
              <dt>Runtime network</dt>
              <dd>{current.network_access}</dd>
              {current.database_sharing && (
                <>
                  <dt>Database</dt>
                  <dd>{describeSharing(current.database_sharing)}</dd>
                </>
              )}
              <dt>Expires</dt>
              <dd title={formatTimestamp(current.expires_at)}>
                {current.expires_at ? formatRelativeTime(current.expires_at) : 'Never'}
              </dd>
              {current.containers.map((container) => (
                <Fragment key={container.id}>
                  <dt className="mono">{container.service}</dt>
                  <dd>
                    <span className="mono">{container.name}</span> — {container.status}
                  </dd>
                </Fragment>
              ))}
            </dl>

            {/* Publish, sync, and reset all refuse while this holds the sandbox.
                Say so here, where the stop control is. */}
            <p className="status">
              This preview holds the sandbox. Publish, sync, and reset ask for consent
              before stopping it.
            </p>

            <div className="button-row">
              {current.url && (
                <a
                  className="button-link primary"
                  href={current.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open preview
                </a>
              )}
              <button type="button" onClick={() => void keepAlive()} disabled={disabled}>
                Keep running
              </button>
              <button type="button" onClick={() => void showLogs()} disabled={disabled}>
                View live logs
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => setConfirmStop(true)}
                disabled={disabled}
              >
                Stop
              </button>
            </div>

            {remainingSeconds !== null && (
              <p className={`status ${expiryWarning ? 'status-warning' : ''}`}>
                {remainingSeconds === 0
                  ? 'Preview expiry is due. The controller will stop it shortly.'
                  : `Preview stops in ${Math.floor(remainingSeconds / 60)}:${String(
                      remainingSeconds % 60,
                    ).padStart(2, '0')}.`}
                {expiryWarning ? ' Use Keep running to extend it.' : ''}
              </p>
            )}

            {confirmStop && (
              <div className="inline-confirm">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={removeData}
                    onChange={(event) => setRemoveData(event.target.checked)}
                  />
                  Remove other nonpersistent preview data volumes. Ephemeral database
                  data is always removed.
                </label>
                <div className="button-row">
                  <button type="button" onClick={() => setConfirmStop(false)}>
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void stop()}
                    disabled={loading}
                  >
                    {loading ? 'Stopping…' : 'Confirm stop'}
                  </button>
                </div>
              </div>
            )}

            {logs && (
              <div className="preview-logs">
                <div className="section-header">
                  <h3>Live preview logs</h3>
                  <div className="button-row">
                    <button type="button" className="small" onClick={() => void showLogs()}>
                      Refresh
                    </button>
                    <button type="button" className="small" onClick={() => setLogs(null)}>
                      Close
                    </button>
                  </div>
                </div>
                {Object.entries(logs.logs).map(([container, output]) => (
                  <details key={container} open>
                    <summary className="mono">{container}</summary>
                    <pre className="file-content">
                      {output || 'No stdout or stderr output.'}
                    </pre>
                  </details>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

export default SandboxPreviewSection
