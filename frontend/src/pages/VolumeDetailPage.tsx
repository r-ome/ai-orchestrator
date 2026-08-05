import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  fetchManagedVolume,
  stopAttachedContainer,
  type VolumeAttachment,
} from '../api/volumes'
import ConfirmDialog from '../components/ConfirmDialog'
import VolumeFileReader from '../components/VolumeFileReader'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

function VolumeDetailPage() {
  const { volumeName = '' } = useParams()
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchManagedVolume(volumeName, signal),
    [volumeName],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [volumeName])

  const [pending, setPending] = useState<VolumeAttachment | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const stopContainer = async (attachment: VolumeAttachment) => {
    setBusy(true)
    setActionError(null)
    try {
      const result = await stopAttachedContainer(
        volumeName,
        attachment.container_id,
      )
      setNotice(`Stopped container ${result.container_name}.`)
      setPending(null)
      reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to="/volumes/managed">← All managed volumes</Link>
          </p>
          <h1>{volumeName}</h1>
        </div>
        <button type="button" onClick={reload} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </header>

      {notice && <p className="status status-ok">{notice}</p>}

      {error && (
        <p className="status status-error" role="alert">
          Failed to load volume: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading volume…</p>}

      {!error && !loading && data && (
        <>
          <dl className="detail-grid">
            <dt>Driver</dt>
            <dd>{data.driver || '—'}</dd>
            <dt>Scope</dt>
            <dd>{data.scope || '—'}</dd>
            <dt>Created</dt>
            <dd title={formatTimestamp(data.created_at)}>
              {formatRelativeTime(data.created_at)}
            </dd>
            <dt>Mountpoint</dt>
            <dd className="mono">{data.mountpoint || '—'}</dd>
            <dt>Labels</dt>
            <dd className="mono">
              {data.labels && Object.keys(data.labels).length > 0
                ? Object.entries(data.labels)
                    .map(([key, value]) => `${key}=${value}`)
                    .join(', ')
                : '—'}
            </dd>
            <dt>Options</dt>
            <dd className="mono">
              {data.options && Object.keys(data.options).length > 0
                ? Object.entries(data.options)
                    .map(([key, value]) => `${key}=${value}`)
                    .join(', ')
                : '—'}
            </dd>
          </dl>

          <h2>Attached containers</h2>
          {data.attachments.length === 0 ? (
            <p className="status">No container uses this volume.</p>
          ) : (
            <div className="table-wrapper">
              <table className="volumes-table">
                <thead>
                  <tr>
                    <th>Container</th>
                    <th>ID</th>
                    <th>Status</th>
                    <th>Destination</th>
                    <th>Access</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.attachments.map((attachment) => (
                    <tr
                      key={`${attachment.container_id}:${attachment.destination}`}
                    >
                      <td>{attachment.container_name}</td>
                      <td className="mono">{attachment.container_id}</td>
                      <td>{attachment.container_status}</td>
                      <td className="mono">{attachment.destination}</td>
                      <td>{attachment.read_write ? 'rw' : 'ro'}</td>
                      <td>
                        <button
                          type="button"
                          className="danger small"
                          disabled={attachment.container_status !== 'running'}
                          onClick={() => setPending(attachment)}
                        >
                          Stop
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2>Read a file</h2>
          <VolumeFileReader
            volumeName={volumeName}
            attachments={data.attachments}
          />
        </>
      )}

      {pending && (
        <ConfirmDialog
          title="Stop this container?"
          confirmPhrase={pending.container_name}
          confirmLabel="Stop container"
          busy={busy}
          error={actionError}
          onCancel={() => {
            setPending(null)
            setActionError(null)
          }}
          onConfirm={() => stopContainer(pending)}
        >
          <p>
            This stops <strong>{pending.container_name}</strong> with a
            10-second timeout. Anything it is serving goes offline until you
            restart it.
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default VolumeDetailPage
