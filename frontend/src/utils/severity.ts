/**
 * One pill colour for every finding severity, across both vocabularies.
 *
 * The backend speaks three severity scales. Planning reviewer findings use
 * `blocking | major | minor`; integration review findings and plan risks use
 * `high | medium | low`. The first two are the same concept in different words
 * — both are review findings, and each gates approval on its top two rungs —
 * so they render as the same three rungs here.
 *
 * This exists because three of the four finding lists used to hardcode
 * `pill warn`, which drew a `blocking` finding in the same amber as a `minor`
 * one and so said nothing.
 */

/** How serious, on a shared 3..1 ladder. Unknown words rank below `low`. */
const SEVERITY_RANK: Record<string, number> = {
  blocking: 3,
  high: 3,
  major: 2,
  medium: 2,
  minor: 1,
  low: 1,
}

/** Pill class per rung. The word inside still carries the meaning. */
const RANK_PILL: Record<number, string> = {
  3: 'err',
  2: 'warn',
  1: 'muted',
}

/** Rank of a severity word, or 0 when the backend sends one we do not know. */
export function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? 0
}

/**
 * The `pill` modifier class for a severity word.
 *
 * Returns `''` for an unknown word, which leaves a plain pill rather than
 * asserting a seriousness we cannot read.
 */
export function severityPill(severity: string): string {
  return RANK_PILL[severityRank(severity)] ?? ''
}
