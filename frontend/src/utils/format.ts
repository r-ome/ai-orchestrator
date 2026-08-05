const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB']

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

const RELATIVE_STEPS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 7],
  ['week', 4.345],
  ['month', 12],
  ['year', Infinity],
]

/** Docker reports an unset time as its zero value, `0001-01-01T00:00:00Z` —
 *  a running container's FinishedAt, for instance. Treat anything before the
 *  Unix epoch as absent so it renders as an em dash, not "2025 years ago". */
const EARLIEST_REAL_TIME = 0

function parseDate(value: string): Date | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.getTime() < EARLIEST_REAL_TIME ? null : date
}

/** "4 Aug 2026, 15:31:22" in the viewer's locale, or an em dash. */
export function formatTimestamp(value: string): string {
  const date = parseDate(value)
  if (!date) return '—'
  return date.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  })
}

/** "2 minutes ago" / "in 3 hours", or an em dash. */
export function formatRelativeTime(value: string, now: number = Date.now()): string {
  const date = parseDate(value)
  if (!date) return '—'

  let delta = (date.getTime() - now) / 1000
  for (const [unit, step] of RELATIVE_STEPS) {
    if (Math.abs(delta) < step) {
      return RELATIVE.format(Math.round(delta), unit)
    }
    delta /= step
  }
  return RELATIVE.format(Math.round(delta), 'year')
}

/** "1m 23s" between two timestamps, counting to `now` while still running. */
export function formatDuration(
  startValue: string,
  endValue: string,
  now: number = Date.now(),
): string {
  const start = parseDate(startValue)
  if (!start) return '—'
  const end = parseDate(endValue)
  const elapsed = Math.max(
    0,
    Math.round(((end ? end.getTime() : now) - start.getTime()) / 1000),
  )

  if (elapsed < 60) return `${elapsed}s`
  const minutes = Math.floor(elapsed / 60)
  const seconds = elapsed % 60
  if (minutes < 60) return `${minutes}m ${seconds}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

/** Matches the backend's `_format_bytes` so client-derived values read the
 *  same as the ones the API sends pre-formatted. */
export function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    UNITS.length - 1,
  )
  const value = bytes / 1024 ** exponent
  return `${value.toFixed(exponent === 0 ? 0 : 2)} ${UNITS[exponent]}`
}
