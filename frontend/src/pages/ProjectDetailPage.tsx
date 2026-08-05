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
  const [summonOpen, setSummonOpen] = useState(false)

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
      <header className="page-header project-detail-header">
        <div>
          <p className="breadcrumb">
            <Link to="/projects">Projects</Link>
            <span className="breadcrumb-separator" aria-hidden="true">
              /
            </span>
            <span className="breadcrumb-current" aria-current="page">
              {data?.name ?? projectName}
            </span>
          </p>
          <h1>{data?.name ?? projectName}</h1>
        </div>
        <div className="button-row">
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
        </div>
      </header>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load project: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading project…</p>}

      {!error && !loading && data && (
        <>
          <div className="detail-status-row">
            <span className={`pill ${data.ready ? 'ok' : 'muted'}`}>
              {data.ready ? '● Ready' : '○ Not ready'}
            </span>
            <CopyStatusBadge status={data.copy_status} />
            <span className="mono" title={formatTimestamp(data.created_at)}>
              registered {formatRelativeTime(data.created_at)}
            </span>
          </div>

          <div className="metric-strip">
            <div className="card metric-card">
              <div className="section-heading">Sandbox ID</div>
              <div className="metric-value">{data.sandbox_id}</div>
            </div>
            <div className="card metric-card">
              <div className="section-heading">Volume</div>
              <div className="metric-value">
                <Link
                  to={`/volumes/managed/${encodeURIComponent(data.volume_name)}`}
                >
                  {data.volume_name}
                </Link>
              </div>
            </div>
            <div className="card metric-card">
              <div className="section-heading">Copy mode</div>
              <div className="metric-value">{data.copy_mode || '—'}</div>
            </div>
            <div className="card metric-card">
              <div className="section-heading">Mountpoint</div>
              <div className="metric-value">{data.mountpoint || '—'}</div>
            </div>
          </div>

          <ProjectAgentsSection
            projectName={data.name}
            projectReady={data.ready}
            summonOpen={summonOpen}
            onSummonOpen={() => setSummonOpen(true)}
            onSummonClose={() => setSummonOpen(false)}
          />

          <ProjectPreviewSection
            projectName={data.name}
            projectReady={data.ready}
          />

          <div className="project-support-grid">
            <ProjectSecretsSection
              projectName={data.name}
              projectReady={data.ready}
            />
            <ProjectDatabaseSharingSection
              projectName={data.name}
              projectReady={data.ready}
            />
          </div>

          <div className="card">
            <div className="card-header">
              <h2>Details</h2>
            </div>
            <dl className="detail-grid">
              <dt>Source folder</dt>
              <dd className="mono">{data.source_path || '—'}</dd>
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
            </dl>
          </div>

          <div className="card">
            <div className="card-header">
              <h2>Copy job</h2>
            </div>
            <div className="card-body">
              {job.error && (
                <p className="status status-error" role="alert">
                  Failed to load copy job: {job.error}
                </p>
              )}

              {!job.error && !job.data && !job.loading && (
                <p className="status">
                  This project has no copy job recorded. It was registered
                  before the backend started writing job metadata into the
                  volume.
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
                This sandbox is a one-time snapshot of the original host
                folder. Later edits to the source folder do not sync. To
                remove a sandbox, delete its volume from the managed volumes
                page.
              </p>
            </div>
          </div>
        </>
      )}

      {showLog && jobId && (
        <CopyLogModal jobId={jobId} onClose={() => setShowLog(false)} />
      )}
    </section>
  )
}

export default ProjectDetailPage
