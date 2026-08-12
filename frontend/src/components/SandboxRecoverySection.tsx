import { useState } from 'react'
import { type Sandbox } from '../api/sandboxes'
import ConfirmDialog from './ConfirmDialog'

interface SandboxRecoverySectionProps {
  sandbox: Sandbox
  /** Any lifecycle action is running; every control disables. */
  busy: boolean
  resetPending: boolean
  resumePending: boolean
  actionError: string | null
  onResetDatabase: (stopBlockingPreview: boolean) => Promise<boolean>
  onResume: () => Promise<boolean>
}

function SandboxRecoverySection({
  sandbox,
  busy,
  resetPending,
  resumePending,
  actionError,
  onResetDatabase,
  onResume,
}: SandboxRecoverySectionProps) {
  const [resetOpen, setResetOpen] = useState(false)
  const [stopBlockingPreview, setStopBlockingPreview] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const hasDatabase = sandbox.db_engine !== 'none'

  const resetDatabase = async () => {
    if (!await onResetDatabase(stopBlockingPreview)) return
    setStatus('Database reset completed.')
    setResetOpen(false)
  }

  const resume = async () => {
    if (!await onResume()) return
    setStatus('Sandbox resources converged.')
  }

  return (
    <>
      <div className="card">
        <div className="card-header"><h2>Recovery</h2></div>
        <div className="card-body">
          {sandbox.pending_base_commit && (
            <p className="status status-error" role="alert">
              A sync was interrupted at <span className="mono">{sandbox.pending_base_commit}</span>. {hasDatabase ? 'Reset database is the recovery path.' : 'Resume is the recovery path.'}
            </p>
          )}
          {hasDatabase && (
            <>
              <p className="status">
                Reset database drops and rebuilds this sandbox database from its confirmed engine, then reruns migrations and seeds. All sandbox database data is lost.
              </p>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={stopBlockingPreview}
                  onChange={(event) => setStopBlockingPreview(event.target.checked)}
                  disabled={busy}
                />
                Stop a running preview before resetting the database
              </label>
              <div className="button-row">
                <button type="button" className="danger" onClick={() => setResetOpen(true)} disabled={busy}>
                  Reset database
                </button>
              </div>
            </>
          )}
          <p className="status">
            Resume converges missing resources without replacing workspace state. It is safe to retry.
          </p>
          <div className="button-row">
            <button type="button" className="primary" onClick={resume} disabled={busy}>
              {resumePending ? 'Resuming…' : 'Resume'}
            </button>
          </div>
          {status && <p className="status status-ok">{status}</p>}
        </div>
      </div>

      {hasDatabase && resetOpen && (
        <ConfirmDialog
          title="Reset this sandbox database?"
          confirmPhrase={`RESET ${sandbox.feature_key || sandbox.sandbox_id}`}
          confirmLabel="Reset database"
          busy={resetPending}
          error={actionError}
          onCancel={() => setResetOpen(false)}
          onConfirm={resetDatabase}
        >
          <p>
            This drops and rebuilds the database from its confirmed engine, then reruns migrations and seeds. All data in the sandbox database is lost.
          </p>
        </ConfirmDialog>
      )}
    </>
  )
}

export default SandboxRecoverySection
