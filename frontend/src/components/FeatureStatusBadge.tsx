import type { FeatureStatus } from '../api/planning'

const LABELS: Record<FeatureStatus, string> = {
  clarifying: 'Clarifying',
  awaiting_confirmation: 'Awaiting your confirmation',
  planning: 'Planning',
  under_review: 'Under review',
  plan_ready: 'Plan ready',
  building: 'Building',
  blocked: 'Blocked',
  in_review: 'In review',
  approved: 'Approved',
  published: 'Published',
  merged: 'Merged',
  abandoned: 'Abandoned',
  review_limit_reached: 'Review limit reached',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

/** Status carries an icon and a word, never color alone. */
const ICONS: Record<FeatureStatus, string> = {
  clarifying: '◷',
  awaiting_confirmation: '?',
  planning: '✦',
  under_review: '⌕',
  plan_ready: '✓',
  building: '⚒',
  blocked: '!',
  in_review: '⌕',
  approved: '✓',
  published: '↗',
  merged: '✓',
  abandoned: '—',
  review_limit_reached: '!',
  failed: '✗',
  cancelled: '—',
}

const PILL_CLASSES: Record<FeatureStatus, string> = {
  clarifying: '',
  awaiting_confirmation: 'warn',
  planning: '',
  under_review: '',
  plan_ready: 'ok',
  building: '',
  blocked: 'err',
  in_review: 'warn',
  approved: 'ok',
  published: 'ok',
  merged: 'ok',
  abandoned: 'muted',
  review_limit_reached: 'warn',
  failed: 'err',
  cancelled: 'muted',
}

function FeatureStatusBadge({ status }: { status: FeatureStatus }) {
  return (
    <span className={`pill feature-status-badge ${PILL_CLASSES[status]}`}>
      <span aria-hidden="true">{ICONS[status]}</span> {LABELS[status]}
    </span>
  )
}

export default FeatureStatusBadge
