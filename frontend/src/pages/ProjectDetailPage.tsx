import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchCopyJob, fetchProject } from '../api/projects'
import CopyLogModal from '../components/CopyLogModal'
import CopyStatusBadge from '../components/CopyStatusBadge'
import ProjectAgentsSection from '../components/ProjectAgentsSection'
import ProjectDatabaseSharingSection from '../components/ProjectDatabaseSharingSection'
import ProjectPreviewSection from '../components/ProjectPreviewSection'
import ProjectSecretsSection from '../components/ProjectSecretsSection'
import { useApiResource } from '../hooks/useApiResource'
import {
  formatDuration,
  formatRelativeTime,
  formatTimestamp,
} from '../utils/format'

function ProjectDetailPage() {
  const { projectName = '' } = useParams()
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchProject(projectName, signal),
    [projectName],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [projectName])

  const jobId = data?.copy_job_id ?? ''
  const jobFetcher = useCallback(
    (signal: AbortSignal) =>
      jobId ? fetchCopyJob(jobId, signal) : Promise.resolve(null),
    [jobId],
  )
  const job = useApiResource(jobFetcher, [jobId])
  const reloadJob = job.reload
  const [showLog, setShowLog] = useState(false)

  const copyIsActive =
    data?.copy_status === 'queued' || data?.copy_status === 'copying'

  // Poll while the copy is still running so status and logs stay current.
  useEffect(() => {
    if (!copyIsActive) return

    const timer = window.setInterval(() => {
      reload()
      reloadJob()
    }, 1_000)
    return () => window.clearInterval(timer)
  }, [copyIsActive, reload, reloadJob])

  return (
    <section>
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to="/projects">← All projects</Link>
          </p>
          <h1>{data?.name ?? projectName}</h1>
        </div>
        <button
          type="button"
          onClick={() => {
            reload()
            reloadJob()
          }}
          disabled={loading}
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </header>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load project: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading project…</p>}

      {!error && !loading && data && (
        <>
          <dl className="detail-grid">
            <dt>Sandbox ID</dt>
            <dd className="mono">{data.sandbox_id}</dd>
            <dt>Source folder</dt>
            <dd className="mono">{data.source_path || '—'}</dd>
            <dt>Volume</dt>
            <dd className="mono">
              <Link
                to={`/volumes/managed/${encodeURIComponent(data.volume_name)}`}
              >
                {data.volume_name}
              </Link>
            </dd>
            <dt>Registered</dt>
            <dd title={formatTimestamp(data.created_at)}>
              {formatRelativeTime(data.created_at)}
            </dd>
            <dt>Copy mode</dt>
            <dd>{data.copy_mode || '—'}</dd>
            <dt>Copy status</dt>
            <dd>
              <CopyStatusBadge status={data.copy_status} />
            </dd>
            <dt>Ready to use</dt>
            <dd>{data.ready ? 'Yes' : 'No'}</dd>
            <dt>Copy job</dt>
            <dd className="mono">{data.copy_job_id || '—'}</dd>
            <dt>Excluded folders</dt>
            <dd className="mono">
              {data.excluded_directories.join(', ') || '—'}
            </dd>
            <dt>Files copied</dt>
            <dd>{data.file_count}</dd>
            <dt>Size copied</dt>
            <dd className="mono">{data.copied_size}</dd>
            <dt>Driver</dt>
            <dd>{data.driver || '—'}</dd>
            <dt>Mountpoint</dt>
            <dd className="mono">{data.mountpoint || '—'}</dd>
          </dl>

          <ProjectDatabaseSharingSection
            projectName={data.name}
            projectReady={data.ready}
          />

          <ProjectSecretsSection
            projectName={data.name}
            projectReady={data.ready}
          />

          <ProjectAgentsSection
            projectName={data.name}
            projectReady={data.ready}
          />

          <ProjectPreviewSection
            projectName={data.name}
            projectReady={data.ready}
          />

          <h2>Copy job</h2>

          {job.error && (
            <p className="status status-error" role="alert">
              Failed to load copy job: {job.error}
            </p>
          )}

          {!job.error && !job.data && !job.loading && (
            <p className="status">
              This project has no copy job recorded. It was registered before
              the backend started writing job metadata into the volume.
            </p>
          )}

          {!job.error && job.data && (
            <>
              <dl className="detail-grid">
                <dt>Job ID</dt>
                <dd className="mono">{job.data.job_id}</dd>
                <dt>Status</dt>
                <dd>
                  <CopyStatusBadge status={job.data.status} />
                </dd>
                <dt>Docker status</dt>
                <dd>{job.data.docker_status || '—'}</dd>
                <dt>Created</dt>
                <dd title={formatTimestamp(job.data.created_at)}>
                  {formatRelativeTime(job.data.created_at)}
                </dd>
                <dt>Started</dt>
                <dd title={formatTimestamp(job.data.started_at)}>
                  {formatRelativeTime(job.data.started_at)}
                </dd>
                <dt>Finished</dt>
                <dd title={formatTimestamp(job.data.finished_at)}>
                  {job.data.finished_at
                    ? formatRelativeTime(job.data.finished_at)
                    : '—'}
                </dd>
                <dt>Duration</dt>
                <dd>
                  {formatDuration(job.data.started_at, job.data.finished_at)}
                </dd>
                <dt>Exit code</dt>
                <dd>
                  {job.data.exit_code === null ? '—' : job.data.exit_code}
                </dd>
              </dl>

              <button type="button" onClick={() => setShowLog(true)}>
                View log
              </button>

              {job.data.error && (
                <p className="status status-error" role="alert">
                  {job.data.error}
                </p>
              )}

              {copyIsActive && (
                <p className="status">Refreshing every second.</p>
              )}
            </>
          )}

          <p className="status">
            This sandbox is a one-time snapshot of the original host folder.
            Later edits to the source folder do not sync. To remove a sandbox,
            delete its volume from the managed volumes page.
          </p>
        </>
      )}

      {showLog && jobId && (
        <CopyLogModal jobId={jobId} onClose={() => setShowLog(false)} />
      )}
    </section>
  )
}

export default ProjectDetailPage
