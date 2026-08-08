/** How one line of a plan changed between two revisions. */
export type DiffOp = 'context' | 'added' | 'removed' | 'skip'

export interface DiffLine {
  op: DiffOp
  /** The line itself, or the empty string on a `skip` marker. */
  text: string
  /** On a `skip` marker, how many unchanged lines it stands for. */
  count?: number
}

/**
 * Above this many lines on either side the diff is not computed.
 *
 * The table below is quadratic, so a runaway plan would freeze the tab. A plan
 * that long is not readable as a diff anyway.
 */
export const DIFF_LINE_LIMIT = 1200

/** Unchanged lines kept either side of a change, so a hunk reads in context. */
const CONTEXT_LINES = 3

function lcsTable(before: string[], after: string[]): Int32Array {
  const width = after.length + 1
  const table = new Int32Array((before.length + 1) * width)
  for (let i = before.length - 1; i >= 0; i -= 1) {
    for (let j = after.length - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        before[i] === after[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1])
    }
  }
  return table
}

/**
 * Replaces long runs of unchanged lines with a single `skip` marker.
 *
 * A plan revision usually rewrites a few steps and leaves the rest alone, so
 * without this the changes are lost in the lines that stayed the same.
 */
function collapseContext(lines: DiffLine[]): DiffLine[] {
  const keep = lines.map((line) => line.op !== 'context')
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index].op === 'context') continue
    for (
      let near = Math.max(0, index - CONTEXT_LINES);
      near <= Math.min(lines.length - 1, index + CONTEXT_LINES);
      near += 1
    ) {
      keep[near] = true
    }
  }

  const collapsed: DiffLine[] = []
  let skipped = 0
  for (let index = 0; index < lines.length; index += 1) {
    if (keep[index]) {
      if (skipped > 0) {
        collapsed.push({ op: 'skip', text: '', count: skipped })
        skipped = 0
      }
      collapsed.push(lines[index])
    } else {
      skipped += 1
    }
  }
  if (skipped > 0) collapsed.push({ op: 'skip', text: '', count: skipped })
  return collapsed
}

/**
 * A line-level diff of two plan revisions, longest-common-subsequence based.
 *
 * Returns `null` when either side is longer than {@link DIFF_LINE_LIMIT}, and
 * an empty array when the two revisions are identical.
 */
export function diffPlanLines(before: string, after: string): DiffLine[] | null {
  const left = before.split('\n')
  const right = after.split('\n')
  if (left.length > DIFF_LINE_LIMIT || right.length > DIFF_LINE_LIMIT) return null

  const table = lcsTable(left, right)
  const width = right.length + 1
  const lines: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < left.length && j < right.length) {
    if (left[i] === right[j]) {
      lines.push({ op: 'context', text: left[i] })
      i += 1
      j += 1
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      lines.push({ op: 'removed', text: left[i] })
      i += 1
    } else {
      lines.push({ op: 'added', text: right[j] })
      j += 1
    }
  }
  for (; i < left.length; i += 1) lines.push({ op: 'removed', text: left[i] })
  for (; j < right.length; j += 1) lines.push({ op: 'added', text: right[j] })

  if (lines.every((line) => line.op === 'context')) return []
  return collapseContext(lines)
}

/** How many lines the diff adds and removes, for a one-line summary. */
export function diffTally(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const line of lines) {
    if (line.op === 'added') added += 1
    if (line.op === 'removed') removed += 1
  }
  return { added, removed }
}
