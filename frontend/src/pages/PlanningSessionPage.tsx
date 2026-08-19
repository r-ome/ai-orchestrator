import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  cancelPlanningSession,
  confirmPlanningUnderstanding,
  correctPlanningUnderstanding,
  fetchPlanningSession,
  fetchPlanningSessions,
  isPlanningTerminal,
  proceedPlanningSession,
  sendPlanningMessage,
} from '../api/planning'
import {
  fetchSandbox,
  projectLabel,
  type Sandbox,
} from '../api/sandboxes'
import { ApiError } from '../api/client'
import ConfirmDialog from '../components/ConfirmDialog'
import {
  ContextModal,
  DelegationPanel,
  useDelegationWorkspace,
} from '../components/DelegationWorkspace'
import PlanSpecView from '../components/PlanSpecView'
import { PlanningAgentInspector } from '../components/PlanningAgentInspector'
import type { PhaseAgent } from '../components/planningAgentInspectorModel'
import PlanningStatusBadge from '../components/PlanningStatusBadge'
import { useApiResource } from '../hooks/useApiResource'
import { ClarifierPanel } from './ClarifierPanel'
import { PlanReviewPanel } from './PlanReviewPanel'
import {
  groupRounds,
  phaseTab,
  sessionStatusLine,
  splitMessages,
  thinkingRole,
  type PendingDialog,
  type TabId,
} from './planningSessionModel'

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
  ], {
    pollWhile: (data) => !isPlanningTerminal(data.status),
    intervalMs: 2_000,
  })
  const sessionsFetcher = useCallback(
    (signal: AbortSignal) => fetchPlanningSessions(projectName, signal),
    [projectName],
  )
  const sessionsResource = useApiResource(sessionsFetcher, [projectName])
  const [message, setMessage] = useState('')
  const [addingClarification, setAddingClarification] = useState(false)
  const [pendingDialog, setPendingDialog] = useState<PendingDialog>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  // Null until the reader picks a tab; the session's phase chooses until then.
  const [chosenTab, setChosenTab] = useState<TabId | null>(null)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)

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
  const preferredTab = chosenTab ?? (data ? phaseTab(data.status) : 'clarifier')
  const activeTab: TabId = preferredTab
  const isDelegationTab = activeTab === 'items' || activeTab === 'feature-review'
  const implementationItem =
    delegation.runningItem ?? delegation.delegation?.items.find((entry) => entry.runs.length > 0) ?? null
  const implementationRun = implementationItem?.runs[implementationItem.runs.length - 1] ?? null
  const implementationProvider = implementationRun?.provider ?? implementationItem?.routing?.provider ?? null
  const implementationModel = implementationRun?.model ?? implementationItem?.routing?.model ?? null
  const sessionList = useMemo(() => {
    if (!data) return []
    const listed = sessionsResource.data?.sessions ?? []
    return listed.some((session) => session.id === data.id) ? listed : [data]
  }, [data, sessionsResource.data])
  const phaseAgents: Partial<Record<TabId, PhaseAgent[]>> = data
    ? {
        clarifier: [
          {
            id: 'clarifier:clarifier',
            role: 'clarifier',
            label: 'Echo',
            // The sidebar carries the state word only. Provider and model are
            // too long for a 250px rail, and the inspector already lists them.
            detail:
              data.status === 'clarifying' && data.turn_state === 'running'
                ? 'active'
                : data.confirmed
                  ? 'done'
                  : 'pending',
            state:
              data.status === 'clarifying' && data.turn_state === 'running'
                ? 'active'
                : data.confirmed
                  ? 'done'
                  : 'pending',
            provider: data.clarifier_provider,
            model: data.clarifier_model,
          },
        ],
        review: [
          {
            id: 'review:planner',
            role: 'planner',
            label: 'Compass',
            detail:
              data.status === 'planning' && data.turn_state === 'running'
                ? 'active'
                : data.plan_revision > 0
                  ? `rev ${data.plan_revision}`
                  : 'pending',
            state:
              data.status === 'planning' && data.turn_state === 'running'
                ? 'active'
                : data.plan_revision > 0
                  ? 'done'
                  : 'pending',
            provider: data.planner_provider,
            model: data.planner_model,
          },
          {
            id: 'review:reviewer',
            role: 'reviewer',
            label: 'Sentinel',
            detail:
              data.status === 'under_review' && data.turn_state === 'running'
                ? 'reviewing'
                : data.status === 'plan_ready'
                  ? 'approved'
                  : data.status === 'review_limit_reached'
                    ? 'limit'
                    : 'pending',
            state:
              data.status === 'under_review' && data.turn_state === 'running'
                ? 'active'
                : ['plan_ready', 'review_limit_reached'].includes(data.status)
                  ? 'done'
                  : 'pending',
            provider: data.reviewer_provider,
            model: data.reviewer_model,
            reasoningEffort: data.reviewer_reasoning_effort,
          },
        ],
        items: [
          {
            id: 'items:work-item',
            role: 'work-item',
            label: 'Spark',
            detail:
              delegation.delegation?.delegation.status === 'running'
                ? 'active'
                : delegation.delegation?.delegation.status === 'completed'
                  ? 'done'
                  : 'pending',
            state:
              delegation.delegation?.delegation.status === 'running'
                ? 'active'
                : delegation.delegation?.delegation.status === 'completed'
                  ? 'done'
                  : 'pending',
            // TODO(redesign): no session-level work-item routing exists before a run.
            provider: implementationProvider,
            model: implementationModel,
          },
        ],
        'feature-review': [
          {
            id: 'feature-review:reviewer',
            role: 'reviewer',
            label: 'Sentinel',
            detail:
              data.feature_status === 'in_review'
                ? 'reviewing'
                : data.feature_status === 'approved'
                  ? 'approved'
                  : 'pending',
            state:
              data.feature_status === 'in_review'
                ? 'active'
                : data.feature_status === 'approved'
                  ? 'done'
                  : 'pending',
            provider: data.reviewer_provider,
            model: data.reviewer_model,
            reasoningEffort: data.reviewer_reasoning_effort,
          },
        ],
      }
    : {}

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
    setSelectedAgentId(null)
  }, [sessionId])

  const selectPhase = (tab: TabId) => {
    setChosenTab(tab)
    setSelectedAgentId(phaseAgents[tab]?.[0]?.id ?? null)
  }

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

  const phases: { id: TabId; label: string; badge?: string }[] = [
    {
      id: 'clarifier',
      label: 'Clarifier',
      badge: data?.confirmed ? '✓' : undefined,
    },
    {
      id: 'review',
      label: 'Plan & Review',
      badge: data && data.review_turn > 0 ? `round ${data.review_turn}` : undefined,
    },
    { id: 'spec', label: 'Plan Spec' },
    {
      id: 'items',
      label: 'Work Items',
      badge: delegation.tabs.find((tab) => tab.id === 'items')?.badge,
    },
    {
      id: 'feature-review',
      label: 'Feature Review',
      badge: delegation.tabs.find((tab) => tab.id === 'feature-review')?.badge,
    },
    { id: 'preview', label: 'Preview' },
  ]
  const currentPhase: TabId = delegation.preview
    ? 'preview'
    : delegation.delegation
      ? delegation.phaseTab
      : data
        ? phaseTab(data.status)
        : 'clarifier'
  const activePhase = phases.find((phase) => phase.id === activeTab) ?? phases[0]
  const selectedAgent = selectedAgentId
    ? Object.values(phaseAgents)
        .flat()
        .find((agent) => agent?.id === selectedAgentId) ?? null
    : null

  return (
    <section className="planning-session-page">
      <aside className="planning-sidebar" aria-label="Planning navigation">
        <div className="app-rail-brand">
          <div className="app-rail-mark" aria-hidden="true">O</div>
          <div className="app-rail-wordmark">Orchestrator</div>
        </div>
        <p className="planning-sidebar-breadcrumb">
          {/* The backend fills project_name with an opaque id, so the repo
              name from the remote URL is the readable first crumb. */}
          <Link to={projectHref}>{projectCrumb ?? data?.project_name ?? 'Unknown project'}</Link>
          <span aria-hidden="true"> / </span>
          <span>{sandbox?.feature_key ?? sandbox?.feature_title ?? data?.title ?? 'Planning session'}</span>
        </p>
        <section className="planning-sidebar-section" aria-labelledby="planning-sessions-label">
          <h2 id="planning-sessions-label">Sessions</h2>
          <div className="planning-session-links">
            {sessionList.map((session) => (
              <Link
                key={session.id}
                className={session.id === data?.id ? 'is-active' : undefined}
                to={`/sandboxes/${encodeURIComponent(session.sandbox_id)}/plans/${encodeURIComponent(session.id)}`}
                aria-current={session.id === data?.id ? 'page' : undefined}
              >
                <span>{session.title}</span>
                <small>{sessionStatusLine(session)}</small>
              </Link>
            ))}
          </div>
        </section>
        <section className="planning-sidebar-section planning-phase-section" aria-labelledby="planning-phases-label">
          <h2 id="planning-phases-label">Phases</h2>
          <div className="planning-phase-list">
            {/* A phase and the agents that run it share one box. Selecting the
                phase fills that box, so the pair reads as one unit instead of
                a header with a list floating under it. */}
            {phases.map((phase) => {
              const agents = phaseAgents[phase.id] ?? []
              const active = phase.id === activeTab
              const current = phase.id === currentPhase
              return (
                <div
                  key={phase.id}
                  className={`planning-phase${active ? ' is-active' : ''}${current ? ' is-current' : ''}`}
                >
                  <button
                    type="button"
                    className="planning-phase-button"
                    aria-pressed={active}
                    onClick={() => selectPhase(phase.id)}
                  >
                    <span className="planning-phase-row">
                      <span className="planning-phase-icon" aria-hidden="true" />
                      <span>{phase.label}</span>
                      {phase.badge && <small>{phase.badge}</small>}
                    </span>
                  </button>
                  {agents.length > 0 && (
                    <div className="planning-phase-agents">
                      {agents.map((agent) => (
                        <button
                          key={agent.id}
                          type="button"
                          className={
                            selectedAgentId === agent.id
                              ? 'planning-phase-agent-button is-selected'
                              : 'planning-phase-agent-button'
                          }
                          aria-pressed={selectedAgentId === agent.id}
                          onClick={() => {
                            setChosenTab(phase.id)
                            setSelectedAgentId(agent.id)
                          }}
                        >
                          <span className="planning-phase-agent">
                            <span
                              className={`planning-agent-dot is-${agent.state}`}
                              aria-hidden="true"
                            />
                            <span>{agent.label}</span>
                            <small>{agent.detail}</small>
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      </aside>

      <main className="planning-session-center">
        <header className="planning-session-bar">
          <div className="planning-session-bar-info">
            <h1>{activePhase.label}</h1>
            {data && <PlanningStatusBadge status={data.status} />}
          </div>
          {data && (
            <div className="button-row">
              {turnRunning && activeRole && activeRole !== 'Clarifier' && (
                <span className="status">{activeRole} is thinking…</span>
              )}
              <button type="button" onClick={reload} disabled={loading || busy}>
                Refresh
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => {
                  setActionError(null)
                  setPendingDialog('cancel')
                }}
                disabled={terminal || busy}
              >
                Cancel
              </button>
            </div>
          )}
        </header>

        <div className="planning-session-main">

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

          {activeTab === 'clarifier' && (
            <ClarifierPanel
              data={data}
              threads={threads}
              projectName={projectName}
              sessionId={sessionId}
              turnRunning={turnRunning}
              activeRole={activeRole}
              busy={busy}
              composerVisible={composerVisible}
              addingClarification={addingClarification}
              message={message}
              canSend={canSend}
              canProceed={canProceed}
              onConfirmUnderstanding={() =>
                void runAction(() =>
                  confirmPlanningUnderstanding(projectName, sessionId),
                )
              }
              onKeepClarifying={() => {
                setActionError(null)
                setAddingClarification(true)
              }}
              onSubmitMessage={submitMessage}
              onMessageChange={setMessage}
              onProceed={() => {
                setActionError(null)
                setPendingDialog('proceed')
              }}
            />
          )}

          {activeTab === 'review' && (
            <PlanReviewPanel
              data={data}
              showReviewProgress={showReviewProgress}
              review={review}
              projectName={projectName}
              sessionId={sessionId}
              settled={settled}
            />
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

          {activeTab === 'preview' && (
            <div role="tabpanel" id="panel-preview" aria-labelledby="tab-preview">
              {delegation.preview ? (
                <p>
                  Preview is {delegation.preview.status} at{' '}
                  <a href={delegation.preview.url} target="_blank" rel="noreferrer">
                    {delegation.preview.url}
                  </a>
                </p>
              ) : (
                <p className="status">
                  No preview yet. Start one after the feature review completes.
                </p>
              )}
            </div>
          )}
        </>
      )}
        </div>

      {delegation.contextModalOpen && (
        <ContextModal
          workspace={delegation}
          onReady={() => {
            setChosenTab('items')
          }}
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
      </main>
      {data && selectedAgent && (
        <PlanningAgentInspector
          agent={selectedAgent}
          messages={data.messages}
          confirmed={data.confirmed}
          projectName={projectName}
          sessionId={sessionId}
          onClose={() => setSelectedAgentId(null)}
        />
      )}
    </section>
  )
}

export default PlanningSessionPage
