import type { PlanningSessionDetail } from '../api/planning'
import CollapsibleCard from '../components/CollapsibleCard'
import Markdown from '../components/Markdown'
import PlanningTurnCard from '../components/PlanningTurnCard'
import { severityPill } from '../utils/severity'
import {
  providerFor,
  roundVerdict,
  type GroupedReview,
} from './planningSessionModel'

interface PlanReviewPanelProps {
  data: PlanningSessionDetail
  showReviewProgress: boolean
  review: GroupedReview
  projectName: string
  sessionId: string
  settled: boolean
}

export function PlanReviewPanel({
  data,
  showReviewProgress,
  review,
  projectName,
  sessionId,
  settled,
}: PlanReviewPanelProps) {
  return (
    <div role="tabpanel" id="panel-review" aria-labelledby="tab-review">
      {showReviewProgress && (
        <section className="card">
          <div className="card-header">
            <h2>Review progress</h2>
          </div>
          <div className="card-body">
            <p>
              Round {data.review_turn} of {data.max_review_turns}. Current
              revision: {data.plan_revision}. Open findings:{' '}
              {
                data.findings.filter((finding) => finding.status === 'open')
                  .length
              }
              .
            </p>
            {/* The rounds below read as a flat list of cards, which
                hides the loop that produced them. */}
            <p className="status">
              One round is one plan revision and the review it received.
              The planner writes a revision, the reviewer answers it once
              with findings and a verdict, and an unapproved verdict
              starts the next round. From round two on, the planner
              answers the previous round&rsquo;s findings and rewrites the
              plan in the same turn.
            </p>
          </div>
        </section>
      )}

      {data.feature_brief && (
        <CollapsibleCard title="Sent to the planner">
          <p className="status">
            The brief the clarifier froze when planning started. The
            planner works from this text alone, not from the live
            conversation.
          </p>
          <pre className="file-content">{data.feature_brief}</pre>
        </CollapsibleCard>
      )}

      {review.preamble.map((entry) => (
        <PlanningTurnCard
          key={entry.sequence}
          message={entry}
          projectName={projectName}
          sessionId={sessionId}
          provider={providerFor(data, entry)}
        />
      ))}

      {review.rounds.length === 0 ? (
        <p className="status">
          The planner has not run yet. It starts once the understanding is
          confirmed.
        </p>
      ) : (
        review.rounds.map((round, index) => {
          const verdict = roundVerdict(round)
          const raised = round.reviewer?.findings.length ?? 0
          const earlier = index > 0 ? review.rounds[index - 1].planner : null
          const previousPlan =
            earlier && earlier.text
              ? {
                  revision: earlier.revision ?? review.rounds[index - 1].number,
                  text: earlier.text,
                }
              : null
          return (
            <CollapsibleCard
              key={round.key}
              title={`Round ${round.number} · plan revision ${round.planner?.revision ?? round.number}`}
              // The newest round is the one the reader came for, and
              // it is the only one whose outcome may still change.
              defaultOpen={index === review.rounds.length - 1}
              aside={
                <>
                  {raised > 0 && (
                    <span className="pill muted">
                      {raised} finding{raised === 1 ? '' : 's'}
                    </span>
                  )}
                  <span className={`pill ${verdict.tone}`}>
                    {verdict.label}
                  </span>
                </>
              }
            >
              {round.planner && (
                <PlanningTurnCard
                  bare
                  message={round.planner}
                  projectName={projectName}
                  sessionId={sessionId}
                  provider={providerFor(data, round.planner)}
                  previousPlan={previousPlan}
                />
              )}
              {round.reviewer ? (
                <PlanningTurnCard
                  bare
                  message={round.reviewer}
                  projectName={projectName}
                  sessionId={sessionId}
                  provider={providerFor(data, round.reviewer)}
                />
              ) : (
                <p className="status">
                  The reviewer has not answered this revision yet.
                </p>
              )}
              {round.extra.map((entry) => (
                <PlanningTurnCard
                  bare
                  key={entry.sequence}
                  message={entry}
                  projectName={projectName}
                  sessionId={sessionId}
                  provider={providerFor(data, entry)}
                />
              ))}
            </CollapsibleCard>
          )
        })
      )}

      {settled && (
        <section className="card">
          <div className="card-header">
            <h2>Final verdict</h2>
            <span
              className={`pill ${data.status === 'plan_ready' ? 'ok' : 'warn'}`}
            >
              <span aria-hidden="true">
                {data.status === 'plan_ready' ? '✓' : '!'}
              </span>{' '}
              {data.status === 'plan_ready'
                ? 'Approved'
                : 'Review limit reached'}
            </span>
          </div>
          <div className="card-body">
            <p>
              {data.status === 'plan_ready'
                ? `The reviewer approved revision ${data.plan_revision} after ${data.review_turn} of ${data.max_review_turns} rounds.`
                : `The loop stopped at the ${data.max_review_turns}-round limit with revision ${data.plan_revision} unapproved.`}
            </p>
            {data.plan_spec?.reviewer_outcome.summary && (
              <Markdown source={data.plan_spec.reviewer_outcome.summary} />
            )}
            {data.findings.filter((finding) => finding.status === 'open')
              .length > 0 && (
              <>
                <div className="section-heading">Findings left open</div>
                <ul className="kv-rows">
                  {data.findings
                    .filter((finding) => finding.status === 'open')
                    .map((finding) => (
                      <li key={finding.finding_id}>
                        <span className="kv-key">
                          <span className={`pill ${severityPill(finding.severity)}`}>
                            {finding.severity}
                          </span>
                          <span className="mono turn-finding-id">
                            {finding.finding_id}
                          </span>
                        </span>
                        <span className="kv-value">{finding.text}</span>
                      </li>
                    ))}
                </ul>
              </>
            )}
          </div>
        </section>
      )}
    </div>
  )
}
