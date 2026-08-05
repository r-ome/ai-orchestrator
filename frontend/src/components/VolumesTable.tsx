import type { RunningVolume } from '../api/volumes'

interface VolumesTableProps {
  volumes: RunningVolume[]
}

function VolumesTable({ volumes }: VolumesTableProps) {
  return (
    <div className="table-wrapper">
      <table className="chrome-table">
        <thead>
          <tr>
            <th>Container</th>
            <th>Container ID</th>
            <th>Type</th>
            <th>Name</th>
            <th>Source</th>
            <th>Destination</th>
            <th>Driver</th>
            <th>Mode</th>
            <th>Access</th>
          </tr>
        </thead>
        <tbody>
          {volumes.map((volume) => (
            <tr
              key={`${volume.container_id}:${volume.destination}:${volume.source}`}
            >
              <td>{volume.container_name}</td>
              <td className="mono">{volume.container_id}</td>
              <td>{volume.type}</td>
              <td className="mono">{volume.name ?? '—'}</td>
              <td className="mono">{volume.source || '—'}</td>
              <td className="mono">{volume.destination || '—'}</td>
              <td>{volume.driver || '—'}</td>
              <td>{volume.mode || '—'}</td>
              <td>{volume.read_write ? 'rw' : 'ro'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default VolumesTable
