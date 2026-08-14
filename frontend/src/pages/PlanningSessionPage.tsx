import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  cancelPlanningSession,
  confirmPlanningUnderstanding,
  correctPlanningUnderstanding,
  fetchPlanningSession,
  isPlanningTerminal,
  proceedPlanningSession,
  sendPlanningMessage,
  type PlanningMessage,
  type PlanningSessionDetail,
  type PlanningStatus,
} from '../api/planning'
import {
  fetchSandbox,
  projectLabel,
  sandboxLabel,
  type Sandbox,
} from '../api/sandboxes'
import { ApiError } from '../api/client'
import CollapsibleCard from '../components/CollapsibleCard'
import ConfirmDialog from '../components/ConfirmDialog'
import {
  ContextModal,
  DelegationPanel,
  useDelegationWorkspace,
  type DelegationTabId,
} from '../components/DelegationWorkspace'
import Markdown from '../components/Markdown'
import PlanSpecView from '../components/PlanSpecView'
import PlanningRawOutput from '../components/PlanningRawOutput'
import PlanningStatusBadge from '../components/PlanningStatusBadge'
import PlanningTurnCard from '../components/PlanningTurnCard'
import Tabs, { type TabDefinition } from '../components/Tabs'
import { useApiResource } from '../hooks/useApiResource'

type PendingDialog = 'proceed' | 'cancel' | null

/** The planning phases, which own the left half of the tab strip. */
type PlanningTabId = 'clarifier' | 'review' | 'spec'

/**
 * Every phase of the session, planning and delegation alike, in one strip.
 *
 * The delegation phases were their own page. They are the same session though:
 * a plan is only worth reading next to what is being built from it, and the
 * reader who approves a plan is the reader who then runs its work items.
 */
type TabId = 'clarifier' | 'review' | 'spec' | DelegationTabId

function thinkingRole(status: PlanningStatus): string | null {
  if (status === 'clarifying' || status === 'awaiting_confirmation') {
    return 'Clarifier'
  }
  if (status === 'planning') return 'Planner'
  if (status === 'under_review') return 'Plan reviewer'
  return null
}

/**
 * The tab the session's current phase makes most useful.
 *
 * Used only until the reader picks a tab themselves, so an open page follows a
 * running session from clarification through to the finished spec.
 */
function phaseTab(status: PlanningStatus): PlanningTabId {
  if (status === 'plan_ready' || status === 'review_limit_reached') return 'spec'
  if (status === 'planning' || status === 'under_review') return 'review'
  return 'clarifier'
}

interface SplitMessages {
  clarifier: PlanningMessage[]
  review: PlanningMessage[]
}

/**
 * Splits the message log into the clarification thread and the planning thread.
 *
 * A system message belongs to whichever phase produced it, so it lands in the
 * clarification thread until the first planner turn appears and in the planning
 * thread after that. Otherwise a clarifier failure would be recorded on a tab
 * the reader has no reason to open.
 */
function splitMessages(messages: PlanningMessage[]): SplitMessages {
  const ordered = [...messages].sort((left, right) => left.sequence - right.sequence)
  const firstPlanner = ordered.find((entry) => entry.role === 'planner')
  const planningStarts = firstPlanner?.sequence ?? Number.POSITIVE_INFINITY

  const split: SplitMessages = { clarifier: [], review: [] }
  for (const entry of ordered) {
    if (entry.role === 'user' || entry.role === 'clarifier') {
      split.clarifier.push(entry)
    } else if (entry.role === 'planner' || entry.role === 'reviewer') {
      split.review.push(entry)
    } else if (entry.sequence < planningStarts) {
      split.clarifier.push(entry)
    } else {
      split.review.push(entry)
    }
  }
  return split
}

/** One planner revision and the reviewer round that answered it. */
interface ReviewRound {
  key: string
  number: number
  planner: PlanningMessage | null
  reviewer: PlanningMessage | null
  /** System turns, and any second reviewer turn, recorded inside this round. */
  extra: PlanningMessage[]
}

interface GroupedReview {
  /** Turns recorded before the planner's first revision, so before round one. */
  preamble: PlanningMessage[]
  rounds: ReviewRound[]
}

/**
 * Groups the planning thread into rounds, one per planner revision.
 *
 * A round is the unit the loop actually runs in: the planner writes a revision,
 * the reviewer answers it once, and the two either settle or go again. Grouping
 * on the planner turn rather than on the revision number keeps a round intact
 * even when a turn arrives without one.
 */
function groupRounds(messages: PlanningMessage[]): GroupedReview {
  const grouped: GroupedReview = { preamble: [], rounds: [] }

  for (const entry of messages) {
    if (entry.role === 'planner') {
      grouped.rounds.push({
        key: `round-${entry.sequence}`,
        number: entry.revision ?? grouped.rounds.length + 1,
        planner: entry,
        reviewer: null,
        extra: [],
      })
      continue
    }

    const current = grouped.rounds[grouped.rounds.length - 1]
    if (!current) {
      grouped.preamble.push(entry)
    } else if (entry.role === 'reviewer' && current.reviewer === null) {
      current.reviewer = entry
    } else {
      current.extra.push(entry)
    }
  }

  return grouped
}

function roundVerdict(round: ReviewRound): { label: string; tone: string } {
  if (round.reviewer === null || round.reviewer.approved === null) {
    return { label: 'Awaiting review', tone: 'muted' }
  }
  return round.reviewer.approved
    ? { label: 'Approved', tone: 'ok' }
    : { label: 'Changes requested', tone: 'warn' }
}

function providerFor(
  session: PlanningSessionDetail,
  message: PlanningMessage,
): PlanningSessionDetail['clarifier_provider'] {
  if (message.role === 'planner') return session.planner_provider
  if (message.role === 'reviewer') return session.reviewer_provider
  return session.clarifier_provider
}

function PlanningSessionPage() {
  // Planning is reachable from two places, so the route supplies one of two
  // params. Both land in the same API position: for a v1 sandbox that
  // position takes the sandbox ID, which is what the v1 branch of
  // inspect_registered_project expects. Do not "fix" that.
  const {
    sandboxId,
    projectName: localName,
    sessionId = '',
  } = useParams()
  const projectName = sandboxId ?? localName ?? ''
  const [sandbox, setSandbox] = useState<Sandbox | null>(null)
  const projectPath = sandbox
    ? `/sandboxes/${encodeURIComponent(sandbox.sandbox_id)}`
    : `/local/${encodeURIComponent(projectName)}`
  const sandboxCrumb = sandbox ? sandboxLabel(sandbox) : projectName
  const projectCrumb = sandbox ? projectLabel(sandbox.remote_url) : null
  const projectHref = sandbox
    ? `/projects/${encodeURIComponent(sandbox.project_id)}`
    : '/projects'
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchPlanningSession(projectName, sessionId, signal),
    [projectName, sessionId],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [
    projectName,
    sessionId,
  ])
  const [message, setMessage] = useState('')
  const [addingClarification, setAddingClarification] = useState(false)
  const [pendingDialog, setPendingDialog] = useState<PendingDialog>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  // Null until the reader picks a tab; the session's phase chooses until then.
  const [chosenTab, setChosenTab] = useState<TabId | null>(null)

  const terminal = data ? isPlanningTerminal(data.status) : false
  const turnRunning = data?.turn_state === 'running'
  // Proceed anyway is the escape hatch from the questioning, so it belongs to
  // the clarifying composer only. Once a summary is on screen the human can
  // read it and confirm, and "I read your summary but will not endorse it" is
  // not a choice worth offering.
  const canProceed = data !== null && !turnRunning && data.status === 'clarifying'
  const composerVisible = data?.status === 'clarifying' || addingClarification
  const canSend = composerVisible && !turnRunning && !busy && message.trim() !== ''
  const showReviewProgress =
    data !== null &&
    ['planning', 'under_review', 'plan_ready', 'review_limit_reached'].includes(
      data.status,
    )
  const activeRole = data ? thinkingRole(data.status) : null
  const threads = useMemo(
    () => splitMessages(data?.messages ?? []),
    [data?.messages],
  )
  const review = useMemo(() => groupRounds(threads.review), [threads.review])
  const settled =
    data !== null && ['plan_ready', 'review_limit_reached'].includes(data.status)
  // Delegation has nothing to show, and nothing to ask the backend, until the
  // plan it works from has settled.
  const delegation = useDelegationWorkspace(projectName, sessionId, settled)
  const disabledTabs: Record<TabId, boolean> = {
    clarifier: false,
    review: threads.review.length === 0,
    spec: !data?.plan_spec,
    ...delegation.disabledTabs,
  }
  // The phase can point at a tab with nothing in it yet — status turns to
  // `planning` before the planner's first turn lands. Fall back rather than
  // leave a disabled tab selected.
  const preferredTab = chosenTab ?? (data ? phaseTab(data.status) : 'clarifier')
  const activeTab: TabId = disabledTabs[preferredTab] ? 'clarifier' : preferredTab
  const isDelegationTab = activeTab === 'items' || activeTab === 'feature-review'

  useEffect(() => {
    if (!data || isPlanningTerminal(data.status)) return

    const timer = window.setInterval(reload, 2_000)
    return () => window.clearInterval(timer)
  }, [data, reload])

  useEffect(() => {
    const controller = new AbortController()
    setSandbox(null)

    void fetchSandbox(projectName, controller.signal)
      .then(setSandbox)
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        if (err instanceof ApiError && err.status === 404) {
          setSandbox(null)
          return
        }
        setSandbox(null)
      })

    return () => controller.abort()
  }, [projectName])

  useEffect(() => {
    setAddingClarification(false)
    setMessage('')
    setActionError(null)
    setChosenTab(null)
  }, [sessionId])

  const runAction = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setActionError(null)
    try {
      await action()
      setAddingClarification(false)
      setMessage('')
      setPendingDialog(null)
      reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy(false)
    }
  }

  const submitMessage = (event: FormEvent) => {
    event.preventDefault()
    if (!data || !canSend) return

    const text = message.trim()
    void runAction(() =>
      addingClarification
        ? correctPlanningUnderstanding(projectName, sessionId, text)
        : sendPlanningMessage(projectName, sessionId, text),
    )
  }

  const closeDialog = () => {
    if (busy) return
    setPendingDialog(null)
    setActionError(null)
  }

  const tabs: TabDefinition<TabId>[] = [
    {
      id: 'clarifier',
      label: 'Clarifier',
      badge: threads.clarifier.length > 0 ? String(threads.clarifier.length) : undefined,
    },
    {
      id: 'review',
      label: 'Plan & Review',
      badge:
        data && data.plan_revision > 0 ? `rev ${data.plan_revision}` : undefined,
      disabled: disabledTabs.review,
    },
    {
      id: 'spec',
      label: 'Plan Spec',
      disabled: disabledTabs.spec,
    },
    ...delegation.tabs,
  ]

  return (
    <section>
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to="/projects">Projects</Link>
            <span className="breadcrumb-separator" aria-hidden="true">
              /
            </span>
            {projectCrumb && (
              <>
                <Link to={projectHref}>{projectCrumb}</Link>
                <span className="breadcrumb-separator" aria-hidden="true">
                  /
                </span>
              </>
            )}
            <Link to={projectPath}>{sandboxCrumb}</Link>
            <span className="breadcrumb-separator" aria-hidden="true">
              /
            </span>
            <span className="breadcrumb-current" aria-current="page">
              {data?.title ?? 'Planning session'}
            </span>
          </p>
          <h1>{data?.title ?? 'Planning session'}</h1>
        </div>
        {data && (
          <div className="button-row">
            <PlanningStatusBadge status={data.status} />
            {/* The clarifier's own wait is shown at the foot of its thread,
                where the reader is already looking. The header keeps the
                later roles, which have no thread of their own here. */}
            {turnRunning && activeRole && activeRole !== 'Clarifier' && (
              <span className="status">{activeRole} is thinking…</span>
            )}
            <button
              type="button"
              className="danger"
              onClick={() => {
                setActionError(null)
                setPendingDialog('cancel')
              }}
              disabled={terminal || busy}
            >
              Cancel session
            </button>
          </div>
        )}
      </header>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load planning session: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading planning session…</p>}

      {data && (
        <>
          {data.status === 'failed' && data.failure_reason && (
            <p className="status status-error" role="alert">
              Planning failed: {data.failure_reason}
            </p>
          )}

          {actionError && !pendingDialog && (
            <p className="status status-error" role="alert">
              {actionError}
            </p>
          )}

          <Tabs
            label="Planning and delegation phases"
            tabs={tabs}
            active={activeTab}
            onSelect={setChosenTab}
          />

          {activeTab === 'clarifier' && (
            <div role="tabpanel" id="panel-clarifier" aria-labelledby="tab-clarifier">
              <section className="card">
                <div className="card-header">
                  <h2>Conversation with the clarifier</h2>
                </div>
                <div className="card-body">
                  {threads.clarifier.length === 0 ? (
                    <p className="status">No messages have been recorded yet.</p>
                  ) : (
                    <ol className="planning-thread">
                      {threads.clarifier.map((entry) => (
                        <li key={entry.sequence}>
                          <div className="section-heading">
                            {entry.role === 'user'
                              ? 'Human'
                              : entry.role === 'clarifier'
                                ? 'Clarifier'
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
                    {data.status === 'awaiting_confirmation' ? (
                      <div className="button-row">
                        <button
                          type="button"
                          className="primary"
                          onClick={() =>
                            void runAction(() =>
                              confirmPlanningUnderstanding(projectName, sessionId),
                            )
                          }
                          disabled={busy}
                        >
                          Confirm and start planning
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setActionError(null)
                            setAddingClarification(true)
                          }}
                          disabled={busy}
                        >
                          Keep clarifying
                        </button>
                      </div>
                    ) : (
                      data.status !== 'clarifying' && (
                        <span className={`pill ${data.confirmed ? 'ok' : 'warn'}`}>
                          <span aria-hidden="true">{data.confirmed ? '✓' : '!'}</span>{' '}
                          {data.confirmed
                            ? 'Confirmed and sent to the planner'
                            : 'Sent without confirmation'}
                        </span>
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
                    <form onSubmit={submitMessage}>
                      <label className="dialog-field">
                        {addingClarification
                          ? 'Add to the understanding'
                          : 'Your reply'}
                        <textarea
                          value={message}
                          rows={5}
                          maxLength={8000}
                          onChange={(event) => setMessage(event.target.value)}
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
                          onClick={() => {
                            setActionError(null)
                            setPendingDialog('proceed')
                          }}
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
          )}

          {activeTab === 'review' && (
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
                                  <span className="pill warn">{finding.severity}</span>
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
          )}

          {activeTab === 'spec' && (
            <div role="tabpanel" id="panel-spec" aria-labelledby="tab-spec">
              {data.plan_spec ? (
                <PlanSpecView
                  planSpec={data.plan_spec}
                  understanding={data.understanding_summary}
                  // Implementation starts with the context turn, which is a
                  // modal: one decision, then the reader is moved on to the
                  // work items. An existing context skips straight there.
                  onImplementPlan={
                    settled
                      ? () =>
                          delegation.context
                            ? setChosenTab(delegation.phaseTab)
                            : delegation.openContextModal()
                      : undefined
                  }
                />
              ) : (
                <p className="status">
                  No plan spec yet. One is written when the review settles.
                </p>
              )}
            </div>
          )}

          {isDelegationTab && (
            <div
              role="tabpanel"
              id={`panel-${activeTab}`}
              aria-labelledby={`tab-${activeTab}`}
            >
              <DelegationPanel tab={activeTab} workspace={delegation} />
            </div>
          )}
        </>
      )}

      {delegation.contextModalOpen && (
        <ContextModal
          workspace={delegation}
          onReady={() => setChosenTab('items')}
        />
      )}

      {pendingDialog === 'proceed' && (
        <ConfirmDialog
          title="Proceed with planning"
          confirmPhrase="PROCEED"
          confirmLabel="Proceed anyway"
          busy={busy}
          error={actionError}
          onConfirm={() => void runAction(() => proceedPlanningSession(projectName, sessionId))}
          onCancel={closeDialog}
        >
          <p>The planner will work from what has been said so far.</p>
        </ConfirmDialog>
      )}

      {pendingDialog === 'cancel' && (
        <ConfirmDialog
          title="Cancel planning session"
          confirmPhrase=""
          confirmLabel="Cancel session"
          busy={busy}
          error={actionError}
          onConfirm={() => void runAction(() => cancelPlanningSession(projectName, sessionId))}
          onCancel={closeDialog}
        >
          <p>This ends the planning session. It does not change the project.</p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default PlanningSessionPage
