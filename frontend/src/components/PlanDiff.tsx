import { useMemo } from 'react'
import { diffPlanLines, diffTally } from '../utils/planDiff'

interface PlanDiffProps {
  /** The plan text of the previous revision. */
  before: string
  /** The plan text of this revision. */
  after: string
  /** The previous revision's number, used in the summary line. */
  previousRevision: number
}

const OP_CLASS: Record<string, string> = {
  added: 'plan-diff-added',
  removed: 'plan-diff-removed',
  context: 'plan-diff-context',
}

const OP_MARK: Record<string, string> = {
  added: '+',
  removed: '-',
  context: ' ',
}

/**
 * What one plan revision changed against the one before it.
 *
 * The plan itself stays the thing on the page; this sits beside it, closed, for
 * the reader who wants to know what the planner actually moved rather than
 * re-reading the whole plan. Both revisions are already in the message log, so
 * the diff is computed here and costs no request.
 */
function PlanDiff({ before, after, previousRevision }: PlanDiffProps) {
  const lines = useMemo(() => diffPlanLines(before, after), [before, after])

  if (lines === null) {
    return (
      <p className="status">
        The plan is too long to diff against revision {previousRevision}.
      </p>
    )
  }

  if (lines.length === 0) {
    return (
      <p className="status">
        The plan text is unchanged from revision {previousRevision}.
      </p>
    )
  }

  const { added, removed } = diffTally(lines)

  return (
    <details className="plan-diff">
      <summary>
        Changes from revision {previousRevision} (+{added} / −{removed} lines)
      </summary>
      <pre className="plan-diff-body">
        {lines.map((line, index) =>
          line.op === 'skip' ? (
            <span key={index} className="plan-diff-skip">
              {`⋯ ${line.count} unchanged line${line.count === 1 ? '' : 's'}\n`}
            </span>
          ) : (
            <span key={index} className={OP_CLASS[line.op]}>
              {`${OP_MARK[line.op]} ${line.text}\n`}
            </span>
          ),
        )}
      </pre>
    </details>
  )
}

export default PlanDiff
