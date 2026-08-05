import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  fetchContainerDetails,
  fetchContainerFile,
  formatPort,
  stopContainer,
} from '../api/containers'
import ConfirmDialog from '../components/ConfirmDialog'
import ContainerProcessTable from '../components/ContainerProcessTable'
import ContainerShell, { type ShellPhase } from '../components/ContainerShell'
import FileReader from '../components/FileReader'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

function ContainerDetailPage() {
  const { containerId = '' } = useParams()
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchContainerDetails(containerId, signal),
    [containerId],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [containerId])

  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [shellOpen, setShellOpen] = useState(false)
  const [shellPhase, setShellPhase] = useState<ShellPhase>('connecting')
  const [shellError, setShellError] = useState<string | null>(null)
  // Bumping this remounts the terminal, which opens a brand-new shell.
  const [shellGeneration, setShellGeneration] = useState(0)

  const openShell = () => {
    setShellError(null)
    setShellPhase('connecting')
    setShellOpen(true)
    setShellGeneration((generation) => generation + 1)
  }

  const stop = async () => {
    setBusy(true)
    setActionError(null)
    try {
      const result = await stopContainer(containerId)
      setNotice(`Stopped ${result.name}.`)
      setConfirming(false)
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
            <Link to="/containers">← Containers</Link>
          </p>
          <h1>{data?.name ?? containerId}</h1>
        </div>
        <div className="button-row">
          {data?.status === 'running' && (
            <button
              type="button"
              className="danger"
              onClick={() => setConfirming(true)}
            >
              Stop
            </button>
          )}
          <button type="button" onClick={reload} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </header>

      {notice && <p className="status status-ok">{notice}</p>}

      {error && (
        <p className="status status-error" role="alert">
          Failed to load container: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading container…</p>}

      {!error && !loading && data && (
        <>
          <dl className="detail-grid">
            <dt>Status</dt>
            <dd>{data.status}</dd>
            <dt>Image</dt>
            <dd className="mono">{data.image}</dd>
            <dt>Image ID</dt>
            <dd className="mono">{data.image_id || '—'}</dd>
            <dt>Full ID</dt>
            <dd className="mono">{data.id}</dd>
            <dt>Platform</dt>
            <dd>{data.platform || '—'}</dd>
            <dt>Created</dt>
            <dd title={formatTimestamp(data.created)}>
              {formatRelativeTime(data.created)}
            </dd>
            <dt>Started</dt>
            <dd title={formatTimestamp(data.started_at)}>
              {formatRelativeTime(data.started_at)}
            </dd>
            <dt>Finished</dt>
            <dd title={formatTimestamp(data.finished_at)}>
              {formatRelativeTime(data.finished_at)}
            </dd>
            <dt>Restarts</dt>
            <dd>{data.restart_count}</dd>
            <dt>Ports</dt>
            <dd className="mono">
              {data.ports.length === 0
                ? '—'
                : data.ports.map(formatPort).join(', ')}
            </dd>
            <dt>Labels</dt>
            <dd className="mono">
              {Object.keys(data.labels).length === 0
                ? '—'
                : Object.entries(data.labels)
                    .map(([key, value]) => `${key}=${value}`)
                    .join(', ')}
            </dd>
          </dl>

          <h2>Processes</h2>
          {data.status === 'running' ? (
            <ContainerProcessTable containerId={containerId} />
          ) : (
            <p className="status">
              This container is {data.status}, so it runs no processes.
            </p>
          )}

          <h2>Shell</h2>
          {data.status !== 'running' ? (
            <p className="status">
              This container is {data.status}. A shell needs it running.
            </p>
          ) : (
            <>
              <div className="section-toolbar">
                <p className="status">
                  Opens a new shell inside <span className="mono">{data.name}</span>.
                  Leaving this page ends it — nothing you start here survives.
                </p>
                {shellOpen ? (
                  <div className="button-row">
                    <span className={`agent-phase agent-phase-${shellPhase}`}>
                      {shellPhase === 'connecting' && 'Connecting…'}
                      {shellPhase === 'live' && 'Connected'}
                      {shellPhase === 'closed' && 'Disconnected'}
                    </span>
                    {shellPhase === 'closed' && (
                      <button type="button" className="primary" onClick={openShell}>
                        New shell
                      </button>
                    )}
                    <button type="button" onClick={() => setShellOpen(false)}>
                      Close
                    </button>
                  </div>
                ) : (
                  <button type="button" className="primary" onClick={openShell}>
                    Open shell
                  </button>
                )}
              </div>

              {shellError && (
                <p className="status status-error" role="alert">
                  {shellError}
                </p>
              )}

              {shellOpen && (
                <ContainerShell
                  key={`${containerId}-${shellGeneration}`}
                  containerId={containerId}
                  onPhase={setShellPhase}
                  onError={setShellError}
                />
              )}
            </>
          )}

          <h2>Mounts</h2>
          {data.mounts.length === 0 ? (
            <p className="status">No mounts.</p>
          ) : (
            <div className="table-wrapper">
              <table className="volumes-table">
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Name</th>
                    <th>Source</th>
                    <th>Destination</th>
                    <th>Access</th>
                  </tr>
                </thead>
                <tbody>
                  {data.mounts.map((mount) => (
                    <tr key={`${mount.source}:${mount.destination}`}>
                      <td>{mount.type}</td>
                      <td className="mono">
                        {mount.name ? (
                          <Link
                            to={`/volumes/managed/${encodeURIComponent(mount.name)}`}
                          >
                            {mount.name}
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="mono">{mount.source || '—'}</td>
                      <td className="mono">{mount.destination || '—'}</td>
                      <td>{mount.read_write ? 'rw' : 'ro'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2>Networks</h2>
          {data.networks.length === 0 ? (
            <p className="status">No networks.</p>
          ) : (
            <div className="table-wrapper">
              <table className="volumes-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>IP address</th>
                    <th>Gateway</th>
                    <th>MAC</th>
                  </tr>
                </thead>
                <tbody>
                  {data.networks.map((network) => (
                    <tr key={network.name}>
                      <td>{network.name}</td>
                      <td className="mono">{network.ip_address || '—'}</td>
                      <td className="mono">{network.gateway || '—'}</td>
                      <td className="mono">{network.mac_address || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2>Read a file</h2>
          {data.status === 'running' ? (
            <FileReader
              placeholder="/etc/hostname"
              hint="Paths are absolute inside the container. Max 1 MiB."
              onRead={async (path) => {
                const response = await fetchContainerFile(containerId, path)
                return {
                  file: response.file,
                  resolvedPath: response.file.path,
                  via: response.container_name,
                }
              }}
            />
          ) : (
            <p className="status">
              This container is {data.status}. File reads need it running.
            </p>
          )}
        </>
      )}

      {confirming && data && (
        <ConfirmDialog
          title="Stop this container?"
          confirmPhrase={data.name}
          confirmLabel="Stop container"
          busy={busy}
          error={actionError}
          onCancel={() => {
            setConfirming(false)
            setActionError(null)
          }}
          onConfirm={stop}
        >
          <p>
            This stops <strong>{data.name}</strong> with a 10-second timeout.
            Anything it serves goes offline until you restart it.
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default ContainerDetailPage
