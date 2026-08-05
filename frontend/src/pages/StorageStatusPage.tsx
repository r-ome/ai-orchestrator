import { fetchStorageStatus, type StorageUsage } from '../api/volumes'
import StorageBar from '../components/StorageBar'
import { useApiResource } from '../hooks/useApiResource'
import { formatBytes } from '../utils/format'

const CATEGORY_LABELS = {
  images: 'Images',
  containers: 'Containers',
  volumes: 'Local volumes',
  build_cache: 'Build cache',
} as const

function StorageStatusPage() {
  const { data, loading, error, reload } = useApiResource(fetchStorageStatus)

  const rows: Array<[string, StorageUsage]> = data
    ? (
        Object.keys(CATEGORY_LABELS) as Array<keyof typeof CATEGORY_LABELS>
      ).map((key) => [CATEGORY_LABELS[key], data[key]])
    : []

  // Every bar shares one scale, so lengths are comparable across rows.
  const scaleBytes = rows.reduce(
    (max, [, usage]) => Math.max(max, usage.size_bytes),
    0,
  )

  const totalInUseBytes = data
    ? data.total_size_bytes - data.total_reclaimable_bytes
    : 0

  return (
    <section className="operations-page">
      <header className="page-header">
        <h1>Docker storage status</h1>
        <button type="button" onClick={reload} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </header>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load storage status: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading storage status…</p>}

      {!error && !loading && data && (
        <>
          <div className="metric-strip storage-metric-strip">
            <div className="card metric-card storage-metric-card">
              <div className="section-heading">Total Docker storage</div>
              <div className="storage-metric-value">{data.total_size}</div>
            </div>
            <div className="card metric-card storage-metric-card">
              <div className="section-heading">In use</div>
              <div className="storage-metric-value">
                {formatBytes(totalInUseBytes)}
              </div>
            </div>
            <div className="card metric-card storage-metric-card">
              <div className="section-heading">Reclaimable</div>
              <div className="storage-metric-value">
                {data.total_reclaimable}
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-header">
              <div className="card-header-title">
                <h2>Storage by category</h2>
                <span className="pill">{rows.length}</span>
              </div>
            </div>
            <div className="card-body storage-card-legend">
              <ul className="legend">
                <li>
                  <span className="legend-swatch in-use" aria-hidden="true" />
                  In use
                </li>
                <li>
                  <span
                    className="legend-swatch reclaimable"
                    aria-hidden="true"
                  />
                  Reclaimable
                </li>
              </ul>
            </div>

            <div className="table-wrapper">
              <table className="chrome-table storage-table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Total</th>
                  <th>Active</th>
                  <th className="bar-column">Usage</th>
                  <th className="numeric">Size</th>
                  <th className="numeric">Reclaimable</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([label, usage]) => (
                  <tr key={label}>
                    <td>{label}</td>
                    <td className="numeric">{usage.total_count}</td>
                    <td className="numeric">{usage.active_count}</td>
                    <td className="bar-column">
                      <StorageBar
                        inUseBytes={usage.size_bytes - usage.reclaimable_bytes}
                        reclaimableBytes={usage.reclaimable_bytes}
                        scaleBytes={scaleBytes}
                        inUseLabel={formatBytes(
                          usage.size_bytes - usage.reclaimable_bytes,
                        )}
                        reclaimableLabel={usage.reclaimable}
                      />
                    </td>
                    <td className="numeric mono">{usage.size}</td>
                    <td className="numeric mono">{usage.reclaimable}</td>
                  </tr>
                ))}
              </tbody>
              </table>
            </div>

            <div className="card-body storage-card-note">
              <p className="status">
                Bars share one scale, so lengths compare across rows. Docker
                does not count host bind-mount data as Docker-managed storage.
              </p>
            </div>
          </div>
        </>
      )}
    </section>
  )
}

export default StorageStatusPage
