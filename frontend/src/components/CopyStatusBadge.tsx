type CopyStatus = 'queued' | 'copying' | 'completed' | 'failed' | 'unknown'

const LABELS: Record<CopyStatus, string> = {
  queued: 'Queued',
  copying: 'Copying',
  completed: 'Completed',
  failed: 'Failed',
  unknown: 'Unknown',
}

/** Status carries an icon and a word, never color alone. */
const ICONS: Record<CopyStatus, string> = {
  queued: '◷',
  copying: '⟳',
  completed: '✓',
  failed: '✗',
  unknown: '?',
}

function CopyStatusBadge({ status }: { status: CopyStatus }) {
  return (
    <span className={`copy-badge copy-${status}`}>
      <span aria-hidden="true">{ICONS[status]}</span> {LABELS[status]}
    </span>
  )
}

export default CopyStatusBadge
