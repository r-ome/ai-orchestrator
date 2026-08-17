import type { PlanningStatus } from '../api/planning'

const LABELS: Record<PlanningStatus, string> = {
  clarifying: 'Clarifying',
  awaiting_confirmation: 'Awaiting your confirmation',
  planning: 'Planning',
  under_review: 'Under review',
  plan_ready: 'Plan ready',
  review_limit_reached: 'Review limit reached',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

/** Status carries an icon and a word, never color alone. */
const ICONS: Record<PlanningStatus, string> = {
  clarifying: '◷',
  awaiting_confirmation: '?',
  planning: '✦',
  under_review: '⌕',
  plan_ready: '✓',
  review_limit_reached: '!',
  failed: '✗',
  cancelled: '—',
}

const PILL_CLASSES: Record<PlanningStatus, string> = {
  clarifying: '',
  awaiting_confirmation: 'warn',
  planning: '',
  under_review: '',
  plan_ready: 'ok',
  review_limit_reached: 'warn',
  failed: 'err',
  cancelled: 'muted',
}

function PlanningStatusBadge({ status }: { status: PlanningStatus }) {
  return (
    <span
      className={`pill planning-status-badge planning-status-${status} ${PILL_CLASSES[status]}`}
    >
      <span aria-hidden="true">{ICONS[status]}</span> {LABELS[status]}
    </span>
  )
}

export default PlanningStatusBadge
