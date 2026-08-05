import { fetchVolumes } from '../api/volumes'
import VolumesTable from '../components/VolumesTable'
import { useApiResource } from '../hooks/useApiResource'

function VolumesPage() {
  const { data, loading, error, reload } = useApiResource(fetchVolumes)

  return (
    <section className="operations-page">
      <header className="page-header">
        <h1>Running container volumes</h1>
        <button type="button" onClick={reload} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </header>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load volumes: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading volumes…</p>}

      {!error && !loading && data && data.volumes.length === 0 && (
        <p className="status">No volumes attached to running containers.</p>
      )}

      {!error && !loading && data && data.volumes.length > 0 && (
        <div className="card">
          <div className="card-header">
            <div className="card-header-title">
              <h2>Mounted volumes</h2>
              <span className="pill">{data.count}</span>
            </div>
          </div>
          <VolumesTable volumes={data.volumes} />
        </div>
      )}
    </section>
  )
}

export default VolumesPage
