import { useCallback } from 'react'
import { fetchDatabaseSharing } from '../api/previews'
import { useApiResource } from '../hooks/useApiResource'
import { describeSharing } from '../utils/databaseSharing'

interface ProjectDatabaseSharingSectionProps {
  projectName: string
  projectReady: boolean
}

/**
 * Shows the sandbox's database coupling whether or not a preview is running.
 *
 * A sandbox that writes to a sibling's data keeps doing so across restarts, so
 * the fact belongs on the sandbox page, not only in the approval form.
 */
function ProjectDatabaseSharingSection({
  projectName,
  projectReady,
}: ProjectDatabaseSharingSectionProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      projectReady
        ? fetchDatabaseSharing(projectName, signal)
        : Promise.resolve(null),
    [projectName, projectReady],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [
    projectName,
    projectReady,
  ])

  if (!projectReady) return null

  const current = data?.current ?? null
  const guest = current?.sharing === 'shared_data'

  return (
    <section>
      <div className="section-header">
        <h2>Database</h2>
        <button type="button" className="small" onClick={reload} disabled={loading}>
          {loading ? 'Working…' : 'Refresh'}
        </button>
      </div>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load database sharing: {error}
        </p>
      )}

      {!error && !loading && !current && (
        <p className="status">
          This sandbox holds no database on a shared server. A preview with an
          isolated database creates and removes its own server.
        </p>
      )}

      {!error && current && (
        <>
          <p className={`status ${guest ? 'status-warning' : ''}`}>
            {describeSharing(current)}
          </p>
          <dl className="detail-grid">
            <dt>Schema</dt>
            <dd className="mono">{current.schema_name}</dd>
            <dt>Server container</dt>
            <dd className="mono">{current.server_container}</dd>
            <dt>Image</dt>
            <dd className="mono">{current.image}</dd>
            <dt>Data</dt>
            <dd>{current.persistence}</dd>
            <dt>Owned by</dt>
            <dd>
              {current.owner_sandbox_id === current.sandbox_id
                ? 'This sandbox'
                : current.owner_project_name}
            </dd>
          </dl>
        </>
      )}

      {!error && data && data.candidates.length > 0 && (
        <p className="status">
          {data.candidates.length} sibling sandbox(es) of this project hold a
          database a new preview could join:{' '}
          {data.candidates.map((candidate) => candidate.project_name).join(', ')}.
        </p>
      )}
    </section>
  )
}

export default ProjectDatabaseSharingSection
