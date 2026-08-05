import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchProjects } from '../api/projects'
import {
  fetchManagedVolumes,
  pruneVolumes,
  removeVolume,
  type ManagedVolume,
} from '../api/volumes'
import ConfirmDialog from '../components/ConfirmDialog'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'
import { groupVolumesByProject } from '../utils/volumeGroups'

type PendingAction =
  | { kind: 'remove'; volume: ManagedVolume }
  | { kind: 'prune' }
  | null

interface VolumeRowsProps {
  volumes: ManagedVolume[]
  onRemove: (volume: ManagedVolume) => void
}

function VolumeRows({ volumes, onRemove }: VolumeRowsProps) {
  return (
    <div className="table-wrapper">
      <table className="volumes-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Driver</th>
            <th>Scope</th>
            <th>Created</th>
            <th>Attached containers</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {volumes.map((volume) => (
            <tr key={volume.name}>
              <td>
                <Link to={`/volumes/managed/${encodeURIComponent(volume.name)}`}>
                  {volume.name}
                </Link>
              </td>
              <td>{volume.driver || '—'}</td>
              <td>{volume.scope || '—'}</td>
              <td title={formatTimestamp(volume.created_at)}>
                {formatRelativeTime(volume.created_at)}
              </td>
              <td>
                {volume.attachments.length === 0
                  ? 'none'
                  : volume.attachments
                      .map(
                        (attachment) =>
                          `${attachment.container_name} (${attachment.container_status})`,
                      )
                      .join(', ')}
              </td>
              <td>
                <button
                  type="button"
                  className="danger small"
                  onClick={() => onRemove(volume)}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ManagedVolumesPage() {
  const { data, loading, error, reload } = useApiResource(fetchManagedVolumes)
  // Only supplies project names for sandbox-owned volumes, so a failure here
  // degrades the headings instead of the page.
  const projects = useApiResource(fetchProjects)
  const [pending, setPending] = useState<PendingAction>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const grouped = useMemo(
    () =>
      groupVolumesByProject(data?.volumes ?? [], projects.data?.projects ?? []),
    [data, projects.data],
  )

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

  const requestRemove = (volume: ManagedVolume) =>
    setPending({ kind: 'remove', volume })

  return (
    <section>
      <header className="page-header">
        <h1>Docker-managed volumes</h1>
        <div className="button-row">
          <button
            type="button"
            className="danger"
            onClick={() => setPending({ kind: 'prune' })}
            disabled={loading}
          >
            Prune unused
          </button>
          <button type="button" onClick={reload} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </header>

      {notice && <p className="status status-ok">{notice}</p>}

      {error && (
        <p className="status status-error" role="alert">
          Failed to load volumes: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading volumes…</p>}

      {!error && !loading && data && data.volumes.length === 0 && (
        <p className="status">No Docker-managed volumes.</p>
      )}

      {!error && !loading && data && data.volumes.length > 0 && (
        <>
          <p className="status">
            {data.count} volume(s) in {grouped.groups.length} project(s)
          </p>

          {grouped.groups.map((group) => (
            <div key={group.key}>
              <div className="section-header">
                <h2>
                  {group.projectName ? (
                    <Link
                      to={`/projects/${encodeURIComponent(group.projectName)}`}
                    >
                      {group.title}
                    </Link>
                  ) : (
                    group.title
                  )}
                </h2>
                <span className="status">{group.volumes.length} volume(s)</span>
              </div>
              <VolumeRows volumes={group.volumes} onRemove={requestRemove} />
            </div>
          ))}

          {grouped.ungrouped.length > 0 && (
            <div>
              <div className="section-header">
                <h2>Not owned by a project</h2>
                <span className="status">
                  {grouped.ungrouped.length} volume(s)
                </span>
              </div>
              <p className="status">
                Shared volumes such as agent credentials. Projects mount them,
                but no project owns them.
              </p>
              <VolumeRows volumes={grouped.ungrouped} onRemove={requestRemove} />
            </div>
          )}
        </>
      )}

      {pending?.kind === 'remove' && (
        <ConfirmDialog
          title="Delete this volume?"
          confirmPhrase={pending.volume.name}
          confirmLabel="Delete volume"
          busy={busy}
          error={actionError}
          onCancel={closeDialog}
          onConfirm={() =>
            runAction(async () => {
              const result = await removeVolume(pending.volume.name)
              return `Deleted volume ${result.name}.`
            })
          }
        >
          <p>
            This permanently deletes <strong>{pending.volume.name}</strong> and
            everything stored in it. Docker cannot undo this.
          </p>
          {pending.volume.attachments.length > 0 && (
            <p className="status status-error">
              {pending.volume.attachments.length} container(s) still use this
              volume. Docker will refuse the delete until they stop.
            </p>
          )}
        </ConfirmDialog>
      )}

      {pending?.kind === 'prune' && (
        <ConfirmDialog
          title="Prune unused volumes?"
          confirmPhrase="PRUNE"
          confirmLabel="Prune volumes"
          busy={busy}
          error={actionError}
          onCancel={closeDialog}
          onConfirm={() =>
            runAction(async () => {
              const result = await pruneVolumes()
              return result.deleted.length === 0
                ? 'No unused volumes to prune.'
                : `Pruned ${result.deleted.length} volume(s), reclaimed ${result.reclaimed}.`
            })
          }
        >
          <p>
            This permanently deletes every volume no container is using, along
            with all their data. Docker cannot undo this.
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default ManagedVolumesPage
