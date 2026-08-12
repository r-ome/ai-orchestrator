import { Link } from 'react-router-dom'
import type { ProjectCopyJobStatus } from '../api/projects'
import CopyStatusBadge from './CopyStatusBadge'
import {
  formatDuration,
  formatRelativeTime,
  formatTimestamp,
} from '../utils/format'

interface CopyJobsTableProps {
  jobs: ProjectCopyJobStatus[]
  onShowLog: (jobId: string) => void
}

function CopyJobsTable({ jobs, onShowLog }: CopyJobsTableProps) {
  return (
    <div className="table-wrapper">
      <table className="chrome-table">
        <thead>
          <tr>
            <th>Project</th>
            <th>Status</th>
            <th>Job</th>
            <th>Started</th>
            <th>Duration</th>
            <th className="numeric">Exit</th>
            <th>Log</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id}>
              <td>
                <Link to={`/local/${encodeURIComponent(job.project_name)}`}>
                  {job.project_name}
                </Link>
              </td>
              <td>
                <CopyStatusBadge status={job.status} />
              </td>
              <td className="mono">{job.job_id.slice(0, 12)}</td>
              {/* Relative reads faster; the exact time is on hover. */}
              <td title={formatTimestamp(job.started_at)}>
                {formatRelativeTime(job.started_at)}
              </td>
              <td>{formatDuration(job.started_at, job.finished_at)}</td>
              <td className="numeric">
                {job.exit_code === null ? '—' : job.exit_code}
              </td>
              <td>
                <button
                  type="button"
                  className="small"
                  onClick={() => onShowLog(job.job_id)}
                >
                  View log
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default CopyJobsTable
