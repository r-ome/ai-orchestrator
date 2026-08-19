import type { FormEvent } from 'react'
import type { PlanningSessionDetail } from '../api/planning'
import Markdown from '../components/Markdown'
import PlanningRawOutput from '../components/PlanningRawOutput'
import { providerFor, type SplitMessages } from './planningSessionModel'

interface ClarifierPanelProps {
  data: PlanningSessionDetail
  threads: SplitMessages
  projectName: string
  sessionId: string
  turnRunning: boolean
  activeRole: string | null
  busy: boolean
  composerVisible: boolean
  addingClarification: boolean
  message: string
  canSend: boolean
  canProceed: boolean
  onConfirmUnderstanding: () => void
  onKeepClarifying: () => void
  onSubmitMessage: (event: FormEvent<HTMLFormElement>) => void
  onMessageChange: (value: string) => void
  onProceed: () => void
}

export function ClarifierPanel({
  data,
  threads,
  projectName,
  sessionId,
  turnRunning,
  activeRole,
  busy,
  composerVisible,
  addingClarification,
  message,
  canSend,
  canProceed,
  onConfirmUnderstanding,
  onKeepClarifying,
  onSubmitMessage,
  onMessageChange,
  onProceed,
}: ClarifierPanelProps) {
  return (
    <div role="tabpanel" id="panel-clarifier" aria-labelledby="tab-clarifier">
      <section className="planning-thread-panel" aria-label="Conversation with the clarifier">
        <div className="planning-thread-body">
          {threads.clarifier.length === 0 ? (
            <p className="status">No messages have been recorded yet.</p>
          ) : (
            <ol className="planning-thread">
              {threads.clarifier.map((entry) => (
                <li
                  key={entry.sequence}
                  className={`planning-message planning-message-${entry.role}`}
                >
                  <span className="planning-message-avatar" aria-hidden="true">
                    {entry.role === 'user' ? 'U' : entry.role === 'clarifier' ? '◌' : '·'}
                  </span>
                  <div className="planning-message-content">
                    <div className="planning-message-author">
                      {entry.role === 'user'
                        ? 'Human'
                        : entry.role === 'clarifier'
                          ? 'Clarifier · clarifier'
                          : 'System'}
                    </div>
                    {entry.text && <Markdown source={entry.text} />}
                    {entry.questions.length > 0 && (
                      <ol className="planning-questions">
                        {entry.questions.map((question, index) => (
                          <li key={`${entry.sequence}-${index}`}>{question}</li>
                        ))}
                      </ol>
                    )}
                    {entry.has_raw_output && (
                      <PlanningRawOutput
                        projectName={projectName}
                        sessionId={sessionId}
                        sequence={entry.sequence}
                        provider={providerFor(data, entry)}
                        model={entry.model}
                      />
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}
          {turnRunning && activeRole === 'Clarifier' && (
            <p className="status planning-thinking" aria-live="polite">
              <span className="planning-thinking-blip" aria-hidden="true" />
              Clarifier is thinking…
            </p>
          )}
        </div>
      </section>

      {/* The understanding outlives the decision on it. Once planning
          starts it is what the planner was given, so it stays on this
          tab as the record of what the clarifier and the human agreed
          rather than disappearing with the Confirm buttons. */}
      {data.understanding_summary && (
        <section className="card">
          <div className="card-header">
            <h2>Understanding</h2>
            {data.confirmed ? (
              <span className="pill ok">
                <span aria-hidden="true">✓</span> confirmed
              </span>
            ) : data.status === 'awaiting_confirmation' ? (
              <div className="button-row">
                <button
                  type="button"
                  className="primary"
                  onClick={onConfirmUnderstanding}
                  disabled={busy}
                >
                  Confirm and start planning
                </button>
                <button
                  type="button"
                  onClick={onKeepClarifying}
                  disabled={busy}
                >
                  Keep clarifying
                </button>
              </div>
            ) : (
              data.status !== 'clarifying' && (
                <span className="pill warn"><span aria-hidden="true">!</span> Sent without confirmation</span>
              )
            )}
          </div>
          <div className="card-body">
            <Markdown source={data.understanding_summary} />
          </div>
        </section>
      )}

      {composerVisible && (
        <section className="card">
          <div className="card-header">
            <h2>{addingClarification ? 'Clarification' : 'Reply'}</h2>
          </div>
          <div className="card-body">
            {turnRunning && (
              <p className="status">
                The composer is disabled while the current planning turn
                runs.
              </p>
            )}
            <form onSubmit={onSubmitMessage}>
              <label className="dialog-field">
                {addingClarification
                  ? 'Add to the understanding'
                  : 'Your reply'}
                <textarea
                  value={message}
                  rows={5}
                  maxLength={8000}
                  onChange={(event) => onMessageChange(event.target.value)}
                  disabled={turnRunning || busy}
                  required
                />
              </label>
              <div className="button-row">
                <button type="submit" className="primary" disabled={!canSend}>
                  {busy ? 'Sending…' : 'Send'}
                </button>
                <button
                  type="button"
                  onClick={onProceed}
                  disabled={!canProceed || busy}
                >
                  Proceed anyway
                </button>
              </div>
            </form>
          </div>
        </section>
      )}
    </div>
  )
}
