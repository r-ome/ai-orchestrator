import type { PlanRisk, PlanSpec } from '../api/planning'
import CollapsibleCard from './CollapsibleCard'
import Markdown from './Markdown'
import { severityPill, severityRank } from '../utils/severity'

function highestSeverityRisks(risks: PlanRisk[]): PlanRisk[] {
  const highest = Math.max(
    0,
    ...risks.map((risk) => severityRank(risk.severity)),
  )
  return risks.filter((risk) => severityRank(risk.severity) === highest)
}

/** Turns a plan title into a filename stem: lowercase words joined by hyphens. */
function fileStem(title: string): string {
  const stem = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return stem || 'plan'
}

/**
 * Downloads the plan markdown from memory.
 *
 * `plan_markdown` already arrives with the session payload, so the file is
 * built in the browser. No endpoint and no request are involved.
 */
function downloadMarkdown(planSpec: PlanSpec): void {
  const blob = new Blob([planSpec.plan_markdown], {
    type: 'text/markdown;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${fileStem(planSpec.title)}-plan.md`
  link.click()
  URL.revokeObjectURL(url)
}

interface PlanSpecViewProps {
  planSpec: PlanSpec
  onImplementPlan?: () => void
  /**
   * The understanding the clarifier and the human settled on. It heads this
   * view because it is the input the plan was built from, so it is what the
   * rest of the spec below is worth checking against.
   */
  understanding: string
}

function PlanSpecView({ planSpec, understanding, onImplementPlan }: PlanSpecViewProps) {
  const topRisks = highestSeverityRisks(planSpec.risks)
  const { reviewer_outcome: reviewerOutcome } = planSpec
  const roundLabel = reviewerOutcome.rounds === 1 ? 'round' : 'rounds'

  return (
    <>
      <CollapsibleCard
        title="Agreed understanding"
        defaultOpen
        aside={
          <span className={`pill ${reviewerOutcome.approved ? 'ok' : 'warn'}`}>
            <span aria-hidden="true">{reviewerOutcome.approved ? '✓' : '!'}</span>{' '}
            {reviewerOutcome.approved ? 'Reviewer approved' : 'Not approved'}
          </span>
        }
      >
        {understanding ? (
          <Markdown source={understanding} />
        ) : (
          <p className="status">No understanding was recorded for this session.</p>
        )}
        <p className="status plan-spec-meta">
          {planSpec.confirmed_understanding
            ? 'The human confirmed this understanding before planning started.'
            : 'The human proceeded without confirming this understanding.'}{' '}
          {reviewerOutcome.approved
            ? 'The reviewer approved the resulting plan'
            : 'The reviewer did not approve the resulting plan'}{' '}
          after {reviewerOutcome.rounds} {roundLabel}.
        </p>
      </CollapsibleCard>

      {!reviewerOutcome.approved && reviewerOutcome.outstanding_findings.length > 0 && (
        <CollapsibleCard
          title="Outstanding findings"
          defaultOpen
          aside={
            <span className="pill warn">
              {reviewerOutcome.outstanding_findings.length}
            </span>
          }
        >
          <ul className="kv-rows">
            {reviewerOutcome.outstanding_findings.map((finding) => (
              <li key={finding.finding_id}>
                <span className="kv-key">
                  <span className={`pill ${severityPill(finding.severity)}`}>
                    {finding.severity}
                  </span>
                  <span className="mono turn-finding-id">{finding.finding_id}</span>
                </span>
                <span className="kv-value">{finding.text}</span>
              </li>
            ))}
          </ul>
        </CollapsibleCard>
      )}

      <CollapsibleCard title="Specification" defaultOpen>
        <div className="plan-spec-section">
          <div className="section-heading">Scope</div>
          <Markdown source={planSpec.scope} />
        </div>

        <div className="plan-spec-section">
          <div className="section-heading">Approach</div>
          <Markdown source={planSpec.approach} />
        </div>

        <div className="plan-spec-section">
          <div className="section-heading">Components</div>
          {planSpec.components.length > 0 ? (
            <ul className="kv-rows plan-spec-components">
              {planSpec.components.map((component) => (
                <li key={component.name}>
                  <span className="kv-key mono">{component.name}</span>
                  <span className="kv-value">{component.responsibility}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No components were recorded.</p>
          )}
        </div>

        <div className="plan-spec-section">
          <div className="section-heading">Top risks</div>
          {topRisks.length > 0 ? (
            <ul className="kv-rows">
              {topRisks.map((risk) => (
                <li key={`${risk.severity}-${risk.text}`}>
                  <span className="kv-key">
                    <span className={`pill ${severityPill(risk.severity)}`}>
                      {risk.severity}
                    </span>
                  </span>
                  <span className="kv-value">{risk.text}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No risks were recorded.</p>
          )}
        </div>

        {planSpec.open_questions.length > 0 && (
          <div className="plan-spec-section">
            <div className="section-heading">Open questions</div>
            <ul>
              {planSpec.open_questions.map((question) => (
                <li key={question}>{question}</li>
              ))}
            </ul>
          </div>
        )}
      </CollapsibleCard>

      <CollapsibleCard
        title="Final plan"
        aside={
          <span className="button-row">
            {onImplementPlan && (
              <button type="button" className="primary" onClick={onImplementPlan}>
                Implement this plan
              </button>
            )}
            <button type="button" onClick={() => downloadMarkdown(planSpec)}>
              Download markdown
            </button>
          </span>
        }
      >
        <Markdown source={planSpec.plan_markdown} />
      </CollapsibleCard>
    </>
  )
}

export default PlanSpecView
