/* Docker's own state words. Anything else falls back to "unknown" styling
   but keeps the raw text, so a new Docker state still reads correctly. */
const KNOWN = [
  'created',
  'restarting',
  'running',
  'removing',
  'paused',
  'exited',
  'dead',
] as const

type ContainerState = (typeof KNOWN)[number]

/** Status carries an icon and a word, never color alone. */
const ICONS: Record<ContainerState, string> = {
  created: '◷',
  restarting: '⟳',
  running: '●',
  removing: '⟳',
  paused: '❚❚',
  exited: '○',
  dead: '✗',
}

function isKnown(status: string): status is ContainerState {
  return (KNOWN as readonly string[]).includes(status)
}

function ContainerStatusBadge({ status }: { status: string }) {
  const known = isKnown(status)
  const className = known ? `container-${status}` : 'container-unknown'

  return (
    <span className={`container-badge ${className}`}>
      <span aria-hidden="true">{known ? ICONS[status] : '?'}</span> {status}
    </span>
  )
}

export default ContainerStatusBadge
