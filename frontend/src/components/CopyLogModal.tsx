import { useEffect, useRef, useState } from 'react'
import { fetchCopyJob, type ProjectCopyJobStatus } from '../api/projects'
import CopyStatusBadge from './CopyStatusBadge'
import { formatDuration, formatTimestamp } from '../utils/format'

interface CopyLogModalProps {
  jobId: string
  onClose: () => void
}

/**
 * Shows one copy job's log. The backend persists status and log inside the
 * project volume at `.orchestrator/copy-job`, so this works after the helper
 * container is gone. The list endpoint omits `log_tail`, so fetch the
 * single-job endpoint, and keep polling while the job is still running.
 */
function CopyLogModal({ jobId, onClose }: CopyLogModalProps) {
  const [job, setJob] = useState<ProjectCopyJobStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [follow, setFollow] = useState(true)
  const logRef = useRef<HTMLPreElement>(null)

  const live =
    job === null || job.status === 'queued' || job.status === 'copying'

  useEffect(() => {
    const controller = new AbortController()

    fetchCopyJob(jobId, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setJob(result)
        setError(null)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Unknown error')
      })

    return () => controller.abort()
  }, [jobId, tick])

  // Poll only while the copy is still running.
  useEffect(() => {
    if (!live) return
    const timer = window.setInterval(() => setTick((value) => value + 1), 1_000)
    return () => window.clearInterval(timer)
  }, [live])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  // Stick to the bottom as new lines arrive, unless the user scrolled up.
  useEffect(() => {
    const element = logRef.current
    if (element && follow) element.scrollTop = element.scrollHeight
  }, [job?.log_tail, follow])

  const onScroll = () => {
    const element = logRef.current
    if (!element) return
    const atBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 24
    setFollow(atBottom)
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="dialog log-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Copy job log"
      >
        <div className="log-modal-header">
          <div>
            <h2>{job ? `${job.project_name} — copy log` : 'Copy log'}</h2>
            <p className="mono log-modal-id">{jobId}</p>
          </div>
          {job && <CopyStatusBadge status={job.status} />}
        </div>

        {job && (
          <dl className="log-modal-meta">
            <div>
              <dt>Started</dt>
              <dd title={formatTimestamp(job.started_at)}>
                {formatTimestamp(job.started_at)}
              </dd>
            </div>
            <div>
              <dt>{job.finished_at ? 'Finished' : 'Running for'}</dt>
              <dd>
                {job.finished_at
                  ? formatTimestamp(job.finished_at)
                  : formatDuration(job.started_at, '')}
              </dd>
            </div>
            <div>
              <dt>Duration</dt>
              <dd>{formatDuration(job.started_at, job.finished_at)}</dd>
            </div>
            <div>
              <dt>Exit code</dt>
              <dd>{job.exit_code === null ? '—' : job.exit_code}</dd>
            </div>
            <div>
              <dt>Skipped folders</dt>
              <dd>{job.excluded_directories.length}</dd>
            </div>
          </dl>
        )}

        {job && job.excluded_directories.length > 0 && (
          <details className="log-modal-excluded">
            <summary>Folders excluded from this copy</summary>
            <p className="mono">{job.excluded_directories.join(', ')}</p>
          </details>
        )}

        {error && (
          <p className="status status-error" role="alert">
            Failed to load log: {error}
          </p>
        )}

        {!error && !job && <p className="status">Loading log…</p>}

        {job?.error && (
          <p className="status status-error" role="alert">
            {job.error}
          </p>
        )}

        {job && (
          <pre
            className="file-content log-modal-body"
            ref={logRef}
            onScroll={onScroll}
          >
            {job.log_tail || 'No log output yet.'}
          </pre>
        )}

        <div className="dialog-actions log-modal-actions">
          <span className="status log-modal-note">
            {live
              ? follow
                ? 'Live — following new output'
                : 'Live — scroll to the bottom to follow'
              : 'Job finished. Log read from .orchestrator/copy-job in the volume.'}
          </span>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

export default CopyLogModal
