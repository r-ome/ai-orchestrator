import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  fetchAllContainers,
  fetchContainerStatus,
  formatPort,
  pruneContainers,
  removeContainer,
  stopContainer,
  type AllContainersResponse,
  type ContainerResourceStatus,
  type ContainerStatusResponse,
  type RunningContainer,
} from '../api/containers'
import ConfirmDialog from '../components/ConfirmDialog'
import ContainerStatusBadge from '../components/ContainerStatusBadge'
import Meter from '../components/Meter'
import { useApiResource } from '../hooks/useApiResource'
import { formatBytes, formatRelativeTime, formatTimestamp } from '../utils/format'

type PendingAction =
  | { kind: 'stop'; container: RunningContainer }
  | { kind: 'remove'; container: RunningContainer }
  | { kind: 'prune' }
  | null

type Filter = 'all' | 'running' | 'stopped'

const FILTERS: { value: Filter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'stopped', label: 'Not running' },
]

interface ContainersView {
  list: AllContainersResponse
  usage: ContainerStatusResponse | null
  /* Sampling stats runs `docker stats` and can fail on its own. The list is
     the page's reason to exist, so a stats failure degrades to a warning
     instead of blanking the table. */
  usageError: string | null
}

async function fetchContainersView(
  signal: AbortSignal,
): Promise<ContainersView> {
  const [list, usage] = await Promise.all([
    fetchAllContainers(signal),
    fetchContainerStatus(signal).then(
      (result) => ({ ok: true as const, result }),
      (err: unknown) => ({
        ok: false as const,
        message: err instanceof Error ? err.message : 'Unknown error',
      }),
    ),
  ])

  return {
    list,
    usage: usage.ok ? usage.result : null,
    usageError: usage.ok ? null : usage.message,
  }
}

/* The CPU and Memory cells for one row. Stats exist only while a container
   runs, so a missing sample renders as an em dash rather than a zeroed meter.
   Net I/O, block I/O, and PID counts ride along in the cell tooltips — they
   are too wide for their own columns. */
function UsageCells({ stats }: { stats: ContainerResourceStatus | undefined }) {
  if (!stats) {
    return (
      <>
        <td className="usage-column">—</td>
        <td className="usage-column">—</td>
      </>
    )
  }

  const io =
    `${formatBytes(stats.network_received_bytes)} in / ` +
    `${formatBytes(stats.network_sent_bytes)} out · ` +
    `${formatBytes(stats.block_read_bytes)} read / ` +
    `${formatBytes(stats.block_write_bytes)} written · ` +
    `${stats.pids} PIDs`

  return (
    <>
      <td className="usage-column" title={io}>
        <div className="meter-cell">
          <Meter
            percent={stats.cpu_percent}
            label={`CPU ${stats.cpu_percent.toFixed(1)}%`}
          />
          <span className="numeric mono">{stats.cpu_percent.toFixed(1)}%</span>
        </div>
      </td>
      <td className="usage-column" title={io}>
        <div className="meter-cell">
          <Meter
            percent={stats.memory_percent}
            label={`Memory ${stats.memory_usage} of ${stats.memory_limit}`}
          />
          <span className="numeric mono">
            {stats.memory_usage} / {stats.memory_limit}
          </span>
        </div>
      </td>
    </>
  )
}

function ContainersPage() {
  const { data, loading, error, reload } = useApiResource(fetchContainersView)
  const [filter, setFilter] = useState<Filter>('all')
  const [pending, setPending] = useState<PendingAction>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const containers = useMemo(() => data?.list.containers ?? [], [data])
  const usage = data?.usage ?? null
  const runningCount = useMemo(
    () => containers.filter((c) => c.status === 'running').length,
    [containers],
  )
  /* Stats only cover running containers, so the lookup misses on purpose for
     every other state. */
  const usageById = useMemo(() => {
    const map = new Map<string, ContainerResourceStatus>()
    usage?.containers.forEach((entry) => map.set(entry.id, entry))
    return map
  }, [usage])
  const visible = useMemo(() => {
    if (filter === 'running') {
      return containers.filter((c) => c.status === 'running')
    }
    if (filter === 'stopped') {
      return containers.filter((c) => c.status !== 'running')
    }
    return containers
  }, [containers, filter])

  const closeDialog = () => {
    setPending(null)
    setActionError(null)
  }

  const runAction = async (action: () => Promise<string>) => {
    setBusy(true)
    setActionError(null)
    try {
      setNotice(await action())
      closeDialog()
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
        <h1>Containers</h1>
        <div className="button-row">
          <button
            type="button"
            className="danger"
            onClick={() => setPending({ kind: 'prune' })}
            disabled={loading}
          >
            Prune stopped
          </button>
          <button type="button" onClick={reload} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </header>

      {notice && <p className="status status-ok">{notice}</p>}

      {error && (
        <p className="status status-error" role="alert">
          Failed to load containers: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading containers…</p>}

      {!error && !loading && containers.length === 0 && (
        <p className="status">No containers.</p>
      )}

      {!error && !loading && containers.length > 0 && (
        <>
          {data?.usageError && (
            <p className="status status-error" role="alert">
              Resource usage unavailable: {data.usageError}
            </p>
          )}

          {usage && (
            <div className="total-figure">
              <p className="total-label">
                Total CPU across {usage.count} running container(s)
              </p>
              <p className="total-value">
                {usage.total_cpu_percent.toFixed(1)}%
              </p>
              <p className="total-sub">
                {usage.total_memory_usage} memory · {usage.total_pids} PIDs ·{' '}
                {formatBytes(usage.total_network_received_bytes)} in /{' '}
                {formatBytes(usage.total_network_sent_bytes)} out ·{' '}
                {formatBytes(usage.total_block_read_bytes)} read /{' '}
                {formatBytes(usage.total_block_write_bytes)} written
              </p>
            </div>
          )}

          <div className="filter-row" role="group" aria-label="Filter by state">
            {FILTERS.map((option) => (
              <button
                key={option.value}
                type="button"
                className={`filter-chip${filter === option.value ? ' active' : ''}`}
                aria-pressed={filter === option.value}
                onClick={() => setFilter(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>

          <p className="status">
            {containers.length} container(s), {runningCount} running
            {filter === 'all' ? '' : ` — showing ${visible.length}`}
          </p>

          {visible.length === 0 ? (
            <p className="status">No containers match this filter.</p>
          ) : (
            <div className="table-wrapper">
              <table className="volumes-table">
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Name</th>
                    <th>ID</th>
                    <th>Image</th>
                    <th className="usage-column">CPU</th>
                    <th className="usage-column">Memory</th>
                    <th>Created</th>
                    <th>Ports</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((container) => (
                    <tr key={container.id}>
                      <td>
                        <ContainerStatusBadge status={container.status} />
                      </td>
                      <td>
                        <Link
                          to={`/containers/detail/${encodeURIComponent(container.id)}`}
                        >
                          {container.name}
                        </Link>
                      </td>
                      <td className="mono">{container.id}</td>
                      <td className="mono">{container.image}</td>
                      <UsageCells stats={usageById.get(container.id)} />
                      <td title={formatTimestamp(container.created)}>
                        {formatRelativeTime(container.created)}
                      </td>
                      <td className="mono">
                        {container.ports.length === 0
                          ? '—'
                          : container.ports.map(formatPort).join(', ')}
                      </td>
                      <td>
                        <div className="button-row">
                          <button
                            type="button"
                            className="danger small"
                            disabled={container.status !== 'running'}
                            onClick={() =>
                              setPending({ kind: 'stop', container })
                            }
                          >
                            Stop
                          </button>
                          <button
                            type="button"
                            className="danger small"
                            onClick={() =>
                              setPending({ kind: 'remove', container })
                            }
                          >
                            Remove
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {usage && (
            <p className="status">
              CPU and memory are sampled at load for running containers only.
              CPU percent can exceed 100% — it sums across cores. Meters cap at
              100%. Hover a meter for network I/O, block I/O, and PID count.
            </p>
          )}
        </>
      )}

      {pending?.kind === 'stop' && (
        <ConfirmDialog
          title="Stop this container?"
          confirmPhrase={pending.container.name}
          confirmLabel="Stop container"
          busy={busy}
          error={actionError}
          onCancel={closeDialog}
          onConfirm={() =>
            runAction(async () => {
              const result = await stopContainer(pending.container.id)
              return `Stopped ${result.name}.`
            })
          }
        >
          <p>
            This stops <strong>{pending.container.name}</strong> with a
            10-second timeout. Anything it serves goes offline until you restart
            it.
          </p>
        </ConfirmDialog>
      )}

      {pending?.kind === 'remove' && (
        <ConfirmDialog
          title="Remove this container?"
          confirmPhrase={pending.container.name}
          confirmLabel="Remove container"
          busy={busy}
          error={actionError}
          onCancel={closeDialog}
          onConfirm={() =>
            runAction(async () => {
              const result = await removeContainer(pending.container.id)
              return `Removed ${result.name}.`
            })
          }
        >
          <p>
            This permanently removes <strong>{pending.container.name}</strong>{' '}
            and its writable layer. Docker cannot undo this.
          </p>
          {pending.container.status === 'running' && (
            <p className="status status-error">
              This container is running. Docker will refuse the removal until it
              stops — force removal is not offered here.
            </p>
          )}
          <p>Named and anonymous volumes are left in place.</p>
        </ConfirmDialog>
      )}

      {pending?.kind === 'prune' && (
        <ConfirmDialog
          title="Prune stopped containers?"
          confirmPhrase="PRUNE"
          confirmLabel="Prune containers"
          busy={busy}
          error={actionError}
          onCancel={closeDialog}
          onConfirm={() =>
            runAction(async () => {
              const result = await pruneContainers()
              return result.deleted.length === 0
                ? 'No stopped containers to prune.'
                : `Pruned ${result.deleted.length} container(s), reclaimed ${result.reclaimed}.`
            })
          }
        >
          <p>
            This permanently removes every stopped container and its writable
            layer. Running containers are untouched. Docker cannot undo this.
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default ContainersPage
