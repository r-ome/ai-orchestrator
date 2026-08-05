import { useCallback } from 'react'
import { fetchContainerProcesses } from '../api/containers'
import { useApiResource } from '../hooks/useApiResource'

interface ContainerProcessTableProps {
  containerId: string
}

/** Renders `docker top` for a running container. The columns are whatever
 *  Docker reports for the host's `ps`, so they are not hard-coded here. */
function ContainerProcessTable({ containerId }: ContainerProcessTableProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchContainerProcesses(containerId, signal),
    [containerId],
  )
  const { data, loading, refreshing, error, reload } = useApiResource(fetcher, [
    containerId,
  ])

  return (
    <>
      <div className="section-toolbar">
        <p className="status">
          {data ? `${data.count} process${data.count === 1 ? '' : 'es'}` : ''}
        </p>
        <button type="button" onClick={reload} disabled={refreshing}>
          {refreshing ? 'Loading…' : 'Refresh processes'}
        </button>
      </div>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load processes: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading processes…</p>}

      {!error && data && data.count === 0 && (
        <p className="status">No processes reported.</p>
      )}

      {!error && data && data.count > 0 && (
        <div className="table-wrapper">
          <table className="chrome-table">
            <thead>
              <tr>
                {data.titles.map((title) => (
                  <th key={title}>{title}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.processes.map((process, rowIndex) => (
                // Rows carry no stable id: the same PID can appear twice across
                // namespaces, so position is the only unique key here.
                <tr key={`${process[0] ?? ''}-${rowIndex}`}>
                  {process.map((cell, cellIndex) => (
                    <td
                      key={`${data.titles[cellIndex] ?? cellIndex}`}
                      className="mono"
                    >
                      {cell || '—'}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

export default ContainerProcessTable
