import type { AgentProvider } from '../api/agents'
import type { PlanningMessage } from '../api/planning'
import Markdown from './Markdown'
import PlanDiff from './PlanDiff'
import PlanningRawOutput from './PlanningRawOutput'
import { severityPill } from '../utils/severity'

interface PlanningTurnCardProps {
  message: PlanningMessage
  projectName: string
  sessionId: string
  provider: AgentProvider
  /**
   * Drops the card chrome. Rounds already sit in a card of their own, and a
   * card inside a card reads as two boxes rather than one turn.
   */
  bare?: boolean
  /** The previous planner revision, so this one can be shown as a diff. */
  previousPlan?: { revision: number; text: string } | null
}

const RESPONSE_PILL: Record<string, string> = {
  answered: 'ok',
  rejected: 'muted',
}

function turnLabel(message: PlanningMessage): string {
  if (message.role === 'planner') {
    return message.revision === null
      ? 'Planner'
      : `Planner · Revision ${message.revision}`
  }
  // The reviewer turn is always rendered inside the round card it belongs to,
  // which already carries the round number. Repeating it here read as a round
  // nested inside a round.
  if (message.role === 'reviewer') return 'Plan reviewer'
  return 'System'
}

function turnRole(message: PlanningMessage): string {
  if (message.role === 'planner') return 'planner'
  if (message.role === 'reviewer') return 'reviewer'
  return 'system'
}

/**
 * One planner, reviewer or system turn, summarised.
 *
 * The findings and the verdict come from the turn's stored payload, so this is
 * what that round actually said. The current state of the same findings lives
 * in the session's finding ledger, which moves as later rounds resolve them.
 */
function PlanningTurnCard({
  message,
  projectName,
  sessionId,
  provider,
  bare = false,
  previousPlan = null,
}: PlanningTurnCardProps) {
  const isReviewer = message.role === 'reviewer'
  const isPlanner = message.role === 'planner'
  const Wrapper = bare ? 'section' : 'article'

  return (
    <Wrapper className={bare ? 'turn-block' : 'card turn-card'}>
      <div className={bare ? 'turn-block-header' : 'card-header'}>
        <div className="planning-turn-identity">
          <span className={`planning-turn-mark planning-turn-mark-${message.role}`} aria-hidden="true">
            {message.role === 'planner' ? '✦' : message.role === 'reviewer' ? '◌' : '·'}
          </span>
          <div>
            <h3>{turnLabel(message)}</h3>
            <span className="mono planning-turn-model">
              {turnRole(message)} · {message.model || provider}
            </span>
          </div>
        </div>
        {isReviewer && message.approved !== null && (
          <span className={`pill ${message.approved ? 'ok' : 'warn'}`}>
            <span aria-hidden="true">{message.approved ? '✓' : '!'}</span>{' '}
            {message.approved ? 'Approved' : 'Changes requested'}
          </span>
        )}
      </div>
      <div className={bare ? 'turn-block-body' : 'card-body'}>
        {message.findings.length > 0 && (
          <>
            <div className="section-heading">
              Findings raised ({message.findings.length})
            </div>
            <ul className="kv-rows">
              {message.findings.map((finding) => (
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
          </>
        )}

        {message.finding_responses.length > 0 && (
          <>
            <div className="section-heading">
              Responses to findings ({message.finding_responses.length})
            </div>
            <ul className="kv-rows">
              {message.finding_responses.map((response) => (
                <li key={response.finding_id}>
                  <span className="kv-key">
                    <span className={`pill ${RESPONSE_PILL[response.status] ?? ''}`}>
                      {response.status}
                    </span>
                    <span className="mono turn-finding-id">{response.finding_id}</span>
                  </span>
                  <span className="kv-value">{response.rationale}</span>
                </li>
              ))}
            </ul>
          </>
        )}

        {message.text &&
          (isReviewer ? (
            <>
              <div className="section-heading">Verdict</div>
              <Markdown source={message.text} />
            </>
          ) : isPlanner ? (
            <>
              {/* The plan is what the round produced, so it renders in full.
                  The diff sits above it, closed, for the reader who only wants
                  to know what this revision moved. */}
              {previousPlan && (
                <PlanDiff
                  before={previousPlan.text}
                  after={message.text}
                  previousRevision={previousPlan.revision}
                />
              )}
              <div className="section-heading">
                {message.revision === null ? 'Plan' : `Plan · revision ${message.revision}`}
              </div>
              <Markdown source={message.text} />
            </>
          ) : (
            <pre className="file-content">{message.text}</pre>
          ))}

        {message.has_raw_output && (
          <PlanningRawOutput
            projectName={projectName}
            sessionId={sessionId}
            sequence={message.sequence}
            provider={provider}
            model={message.model}
          />
        )}
      </div>
    </Wrapper>
  )
}

export default PlanningTurnCard
