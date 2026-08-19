import { requestFeatureChanges, runIntegrationReview } from '../api/delegation'
import { FeatureCodeDiff } from './FeatureCodeDiff'
import type { DelegationWorkspace } from './DelegationWorkspace'
import TurnConsole from './TurnConsole'
import { selectReviewState } from './delegationWorkspaceModel'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

function changeEvidenceErrors(verification: Record<string, unknown> | null): string[] {
  const evidence = verification?.acceptance_evidence
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return []
  const errors = (evidence as Record<string, unknown>).errors
  return Array.isArray(errors) ? errors.filter((error): error is string => typeof error === 'string') : []
}

export function FeatureReviewPanel({
  workspace,
  changeInstructions,
  setChangeInstructions,
}: {
  workspace: DelegationWorkspace
  changeInstructions: string
  setChangeInstructions: (value: string) => void
}) {
  const {
    projectName,
    sessionId,
    loading,
    error,
    delegation,
    reload,
    busy,
    watching,
    watchTurn,
    previewFeature,
    preview,
    previewLogs,
  } = workspace
  const {
    latestChange,
    runningChange,
    reviewSuperseded,
    featureApproved,
  } = selectReviewState(delegation)

  return (
    <>
      {delegation && (
        <section className="card">
          <div className="card-header">
            <h2>Feature-level review</h2>
            {delegation.review?.status === 'completed' && (
              <span className={`pill ${featureApproved ? 'ok' : 'warn'}`}>
                {featureApproved
                  ? 'Approved'
                  : reviewSuperseded
                    ? 'Review needed'
                    : 'Findings remain'}
              </span>
            )}
          </div>
          <div className="card-body">
            {delegation.review ? (
              <>
                <p>{delegation.review.summary || delegation.review.error}</p>
                {delegation.review.findings.length > 0 && (
                  <ul className="kv-rows">
                    {delegation.review.findings.map((finding, index) => (
                      <li key={`${finding.text}-${index}`}>
                        <span className="kv-key">
                          <span className="pill warn">{finding.severity}</span>
                          {finding.work_item_keys.join(', ')}
                        </span>
                        <span className="kv-value">{finding.text}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p>
                Review the merged feature against the plan and controller-run
                verification.
              </p>
            )}

            <section className="feature-refinement" aria-labelledby="feature-refinement-heading">
              <div className="section-heading" id="feature-refinement-heading">
                Review and refine
              </div>
              <p className="status">
                Preview the full implementation. If it needs a small update, describe it for
                an agent. The current implementation stays on hold until you approve it.
              </p>
              <div className="button-row">
                <button
                  type="button"
                  disabled={Boolean(busy) || Boolean(runningChange)}
                  onClick={previewFeature}
                >
                  {busy === 'preview-feature'
                    ? 'Preparing preview…'
                    : preview
                      ? 'Rebuild full preview'
                      : 'Preview full implementation'}
                </button>
              </div>
              {preview && (
                <p className="status">
                  Full preview is {preview.status} at{' '}
                  <a href={preview.url} target="_blank" rel="noreferrer">
                    {preview.url}
                  </a>
                  .
                </p>
              )}
              {previewLogs && (
                <div className="delegation-preview-progress">
                  <p className="status">
                    Preview status: <span className="mono">{previewLogs.status}</span>
                  </p>
                  <ol className="preview-progress-events" aria-live="polite">
                    {previewLogs.events.map((event) => (
                      <li key={event.id} className={event.level === 'error' ? 'status-error' : ''}>
                        <span className="mono">{event.step}</span>
                        <span>{event.message}</span>
                        <time dateTime={event.created_at} title={formatTimestamp(event.created_at)}>
                          {formatRelativeTime(event.created_at)}
                        </time>
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {delegation.changes.length > 0 && (
                <ol className="feature-change-history">
                  {delegation.changes.map((change) => (
                    <li key={change.id}>
                      <span className={`pill ${change.status === 'completed' ? 'ok' : change.status === 'failed' ? 'err' : 'warn'}`}>
                        {change.status === 'awaiting_review' ? 'awaiting review' : change.status}
                      </span>
                      <span>Revision {change.revision}: {change.instructions}</span>
                      {change.status === 'awaiting_review' && (
                        <span className="status">
                          Held until the whole-feature review approves this implementation.
                        </span>
                      )}
                      {changeEvidenceErrors(change.verification).map((error) => (
                        <span key={error} className="status status-warning">{error}</span>
                      ))}
                      {change.error && <span className="status status-error">{change.error}</span>}
                    </li>
                  ))}
                </ol>
              )}

              <label className="field-label" htmlFor="feature-change-instructions">
                Requested changes
              </label>
              <textarea
                id="feature-change-instructions"
                rows={4}
                value={changeInstructions}
                disabled={Boolean(runningChange)}
                placeholder="Example: Reduce the dialog width and clarify the empty-state message."
                onChange={(event) => setChangeInstructions(event.target.value)}
              />
              <div className="button-row">
                <button
                  type="button"
                  disabled={Boolean(busy) || Boolean(runningChange) || !changeInstructions.trim()}
                  onClick={() => {
                    const instructions = changeInstructions.trim()
                    void watchTurn('change', 'change', 'Requested feature changes', () =>
                      requestFeatureChanges(
                        projectName,
                        sessionId,
                        delegation.delegation.id,
                        instructions,
                      ),
                    ).then((jobId) => {
                      if (jobId) setChangeInstructions('')
                    })
                  }}
                >
                  {runningChange ? 'Applying changes…' : 'Request changes'}
                </button>
              </div>
              {watching?.kind === 'change' && (
                <TurnConsole
                  projectName={projectName}
                  sessionId={sessionId}
                  kind="change"
                  jobId={watching.jobId}
                  title={watching.title}
                  onFinished={reload}
                />
              )}
            </section>

            <FeatureCodeDiff
              projectName={projectName}
              sessionId={sessionId}
              delegationId={delegation.delegation.id}
              review={delegation.review}
              revisionKey={`${latestChange?.id ?? 'none'}:${latestChange?.status ?? 'none'}`}
            />
            <button
              type="button"
              className="primary feature-review-action"
              disabled={
                Boolean(busy) ||
                delegation.review?.status === 'generating' ||
                featureApproved
              }
              onClick={() =>
                void watchTurn('review', 'review', 'Feature review', () =>
                  runIntegrationReview(projectName, sessionId, delegation.delegation.id),
                )
              }
            >
              {delegation.review?.status === 'generating'
                ? 'Reviewing feature…'
                : featureApproved
                  ? 'Feature approved'
                  : delegation.review
                    ? 'Run review again'
                    : 'Run feature review'}
            </button>

            {watching?.kind === 'review' && (
              <TurnConsole
                projectName={projectName}
                sessionId={sessionId}
                kind="review"
                jobId={watching.jobId}
                title={watching.title}
                onFinished={reload}
              />
            )}
          </div>
        </section>
      )}

      {!delegation && !loading && !error && (
        <p className="status">
          No feature review yet. Complete the work items before starting this phase.
        </p>
      )}
    </>
  )
}
