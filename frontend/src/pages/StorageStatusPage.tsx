import { useState } from 'react'
import {
  fetchOrphanResources,
  removeOrphanResource,
  type OrphanResource,
} from '../api/sandboxes'
import { fetchStorageStatus, type StorageUsage } from '../api/volumes'
import ConfirmDialog from '../components/ConfirmDialog'
import StorageBar from '../components/StorageBar'
import { useApiResource } from '../hooks/useApiResource'
import { formatBytes, formatRelativeTime } from '../utils/format'

const CATEGORY_LABELS = {
  images: 'Images',
  containers: 'Containers',
  volumes: 'Local volumes',
  build_cache: 'Build cache',
} as const

function StorageStatusPage() {
  const { data, loading, error, reload: reloadStorage } = useApiResource(fetchStorageStatus)
  const orphans = useApiResource(fetchOrphanResources)
  const [pendingRemoval, setPendingRemoval] = useState<OrphanResource | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [removalStatus, setRemovalStatus] = useState<string | null>(null)

  const reload = () => {
    reloadStorage()
    orphans.reload()
  }

  const confirmRemove = async () => {
    if (!pendingRemoval || busy) return
    setBusy(true)
    setActionError(null)
    try {
      const result = await removeOrphanResource(pendingRemoval.resource)
      setRemovalStatus(
        result.removed
          ? `Removed ${pendingRemoval.name}.`
          : 'This resource is now claimed and was not removed.',
      )
      setPendingRemoval(null)
      orphans.reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy(false)
    }
  }

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

      {/* Independent of the storage figures above. A failing `docker system df`
          must not hide unclaimed resources, which is when they matter most. */}
      <div className="card">
        <div className="card-header"><h2>Unclaimed sandbox resources</h2></div>
        <div className="card-body">
          <p className="status">
            These resources are reported, never deleted automatically. Do not use docker volume prune to clean them up because it also deletes live sandbox and agent credential volumes.
          </p>
          {orphans.loading && <p className="status">Loading unclaimed sandbox resources…</p>}
          {orphans.error && <p className="status status-error" role="alert">Failed to load unclaimed sandbox resources: {orphans.error}</p>}
          {!orphans.loading && !orphans.error && orphans.data?.resources.length === 0 && (
            <p className="status status-ok">No unclaimed sandbox resources.</p>
          )}
          {orphans.data && orphans.data.resources.length > 0 && (
            <div className="table-wrapper">
              <table className="chrome-table">
                <thead>
                  <tr>
                    <th>Resource</th>
                    <th>Kind</th>
                    <th>Name</th>
                    <th>Reported</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {orphans.data.resources.map((resource) => (
                    <tr key={resource.resource}>
                      <td className="mono">{resource.resource}</td>
                      <td>{resource.kind}</td>
                      <td className="mono">{resource.name}</td>
                      <td>{formatRelativeTime(resource.reported_at)}</td>
                      <td>
                        <button type="button" className="danger" onClick={() => setPendingRemoval(resource)} disabled={busy}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {removalStatus && <p className="status">{removalStatus}</p>}
        </div>
      </div>

      {pendingRemoval && (
        <ConfirmDialog
          title={`Remove ${pendingRemoval.name}?`}
          confirmPhrase={pendingRemoval.name}
          confirmLabel="Remove resource"
          busy={busy}
          error={actionError}
          onCancel={() => setPendingRemoval(null)}
          onConfirm={confirmRemove}
        >
          <p>
            This permanently removes the unclaimed <span className="mono">{pendingRemoval.resource}</span> resource. Docker cannot undo this.
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default StorageStatusPage
