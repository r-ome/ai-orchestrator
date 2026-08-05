import { useEffect, useState } from 'react'
import { browseFolders, type BrowseResponse } from '../api/projects'

interface FolderPickerProps {
  /** Called with the absolute path of the chosen folder. */
  onSelect: (path: string) => void
  onCancel: () => void
}

/**
 * Browses folders through the backend, which confines every path to the
 * configured project root. A browser file input cannot supply absolute paths,
 * so the server does the listing.
 */
function FolderPicker({ onSelect, onCancel }: FolderPickerProps) {
  const [target, setTarget] = useState<string | undefined>(undefined)
  const [data, setData] = useState<BrowseResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)

    browseFolders(target, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setData(result)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Unknown error')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [target])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onCancel])

  const atRoot = data !== null && data.parent === null

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="dialog picker"
        role="dialog"
        aria-modal="true"
        aria-label="Choose a project folder"
      >
        <h2>Choose a folder</h2>

        <p className="picker-path mono">{data?.path ?? '…'}</p>

        {error && (
          <p className="status status-error" role="alert">
            {error}
          </p>
        )}

        {loading && <p className="status">Loading…</p>}

        {!loading && !error && data && (
          <ul className="picker-list">
            {!atRoot && (
              <li>
                <button
                  type="button"
                  className="picker-row ghost"
                  onClick={() => setTarget(data.parent ?? undefined)}
                >
                  <span className="picker-icon" aria-hidden="true">
                    ↰
                  </span>
                  <span className="picker-name">Up one level</span>
                </button>
              </li>
            )}

            {data.entries.length === 0 && (
              <li className="picker-empty">No subfolders here.</li>
            )}

            {data.entries.map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  className="picker-row ghost"
                  disabled={!entry.has_children}
                  title={
                    entry.has_children
                      ? `Open ${entry.name}`
                      : `${entry.name} has no subfolders`
                  }
                  onClick={() => setTarget(entry.path)}
                >
                  <span className="picker-icon" aria-hidden="true">
                    📁
                  </span>
                  <span className="picker-name">{entry.name}</span>
                </button>
                <button
                  type="button"
                  className="small"
                  onClick={() => onSelect(entry.path)}
                >
                  Select
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="dialog-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!data || atRoot}
            title={
              atRoot
                ? 'The project root itself cannot be registered'
                : undefined
            }
            onClick={() => data && onSelect(data.path)}
          >
            Use this folder
          </button>
        </div>
      </div>
    </div>
  )
}

export default FolderPicker
