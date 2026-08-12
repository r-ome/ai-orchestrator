import { useCallback, useState } from 'react'
import {
  fetchSandboxPublication,
  type PublishSandboxResult,
  type Sandbox,
} from '../api/sandboxes'
import { ApiError } from '../api/client'
import ConfirmDialog from './ConfirmDialog'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime } from '../utils/format'

interface SandboxPublicationSectionProps {
  sandbox: Sandbox
  /** Any lifecycle action is running; every control disables. */
  busy: boolean
  /** This section's own action is running; only it shows progress. */
  pending: boolean
  actionError: string | null
  onPublish: () => Promise<PublishSandboxResult | null>
}

function SandboxPublicationSection({
  sandbox,
  busy,
  pending,
  actionError,
  onPublish,
}: SandboxPublicationSectionProps) {
  // A sandbox has no publication row until its first publish. That 404 is the
  // normal empty state; every other failure is a real error and must show as one.
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      fetchSandboxPublication(sandbox.sandbox_id, signal).catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }),
    [sandbox.sandbox_id],
  )
  const publication = useApiResource(fetcher, [fetcher])
  const [open, setOpen] = useState(false)
  const [publishResult, setPublishResult] = useState<PublishSandboxResult | null>(null)
  const remoteBranch = publication.data?.remote_branch || sandbox.feature_branch || sandbox.sandbox_id

  const publish = async () => {
    const result = await onPublish()
    if (!result) return
    setPublishResult(result)
    setOpen(false)
    publication.reload()
  }

  return (
    <>
      <div className="card">
        <div className="card-header"><h2>Publication</h2></div>
        <div className="card-body">
          {publication.loading && <p className="status">Loading publication…</p>}
          {publication.error && (
            <p className="status status-error" role="alert">
              Failed to load publication: {publication.error}
            </p>
          )}
          {!publication.loading && !publication.error && !publication.data && (
            <p className="status">No publication recorded yet.</p>
          )}
          {publication.data && (
            <>
              <dl className="detail-grid">
                <dt>Remote branch</dt><dd className="mono">{publication.data.remote_branch}</dd>
                <dt>Last pushed commit</dt>
                <dd><span className="mono" title={publication.data.last_pushed_commit ?? ''}>{publication.data.last_pushed_commit?.slice(0, 12) || '—'}</span></dd>
                <dt>Remote branch SHA</dt>
                <dd><span className="mono" title={publication.data.remote_branch_sha ?? ''}>{publication.data.remote_branch_sha?.slice(0, 12) || '—'}</span></dd>
                <dt>Pull request</dt>
                <dd>
                  {publication.data.pr_number === null
                    ? '—'
                    : publication.data.pr_url
                      ? <a href={publication.data.pr_url} target="_blank" rel="noreferrer">#{publication.data.pr_number}</a>
                      : `#${publication.data.pr_number}`}
                </dd>
                <dt>PR state</dt><dd>{publication.data.pr_state || '—'}</dd>
                <dt>Updated</dt><dd>{formatRelativeTime(publication.data.updated_at)}</dd>
              </dl>
              {publication.data.last_error && <p className="status status-error" role="alert">{publication.data.last_error}</p>}
            </>
          )}
          <div className="button-row">
            <button type="button" className="primary" onClick={() => setOpen(true)} disabled={busy}>
              Publish branch
            </button>
          </div>
          {publishResult && (
            <p className="status status-ok">
              {publishResult.pushed ? 'Branch pushed.' : 'No new commit pushed.'}{' '}
              {publishResult.pr_url && publishResult.pr_number !== null
                ? <a href={publishResult.pr_url} target="_blank" rel="noreferrer">Pull request #{publishResult.pr_number}</a>
                : publishResult.pr_number !== null
                  ? `Pull request #${publishResult.pr_number}`
                  : 'No pull request recorded.'}
            </p>
          )}
        </div>
      </div>

      {open && (
        <ConfirmDialog
          title="Publish this branch?"
          confirmPhrase={sandbox.feature_branch || sandbox.sandbox_id}
          confirmLabel="Publish branch"
          busy={pending}
          error={actionError}
          onCancel={() => setOpen(false)}
          onConfirm={publish}
        >
          <p>
            This pushes remote branch <span className="mono">{remoteBranch}</span>. A pull request will be created if none exists.
          </p>
        </ConfirmDialog>
      )}
    </>
  )
}

export default SandboxPublicationSection
