import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  cancelPlanningSession,
  confirmPlanningUnderstanding,
  correctPlanningUnderstanding,
  fetchPlanningMessageRaw,
  fetchPlanningSession,
  fetchPlanningSessions,
  isPlanningTerminal,
  proceedPlanningSession,
  sendPlanningMessage,
  type PlanningMessage,
  type PlanningSession,
  type PlanningSessionDetail,
  type PlanningStatus,
} from '../api/planning'
import {
  fetchSandbox,
  projectLabel,
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
import { useApiResource } from '../hooks/useApiResource'

type PendingDialog = 'proceed' | 'cancel' | null

/**
 * Every phase of the session, planning and delegation alike, in one sidebar.
 *
 * The delegation phases were their own page. They are the same session though:
 * a plan is only worth reading next to what is being built from it, and the
 * reader who approves a plan is the reader who then runs its work items.
 */
type TabId = 'clarifier' | 'review' | 'spec' | DelegationTabId | 'preview'

interface PhaseAgent {
  id: string
  role: InspectorAgentRole
  label: string
  detail: string
  provider: string | null
  model: string | null
  reasoningEffort?: string | null
  state: 'active' | 'done' | 'pending'
}

type InspectorAgentRole = 'clarifier' | 'planner' | 'reviewer' | 'work-item'
type InspectorTab = 'history' | 'raw' | 'controls'

function messageKind(message: PlanningMessage): string {
  if (message.questions.length > 0) return 'question'
  if (message.revision !== null) return 'revision'
  if (message.approved !== null) return 'review'
  return 'understanding'
}

function inspectorStatus(state: PhaseAgent['state']): string {
  if (state === 'active') return 'active'
  if (state === 'done') return 'done'
  return 'pending'
}

function InspectorIcon({ role }: { role: InspectorAgentRole }) {
  if (role === 'clarifier') return <span aria-hidden="true">◌</span>
  if (role === 'planner') return <span aria-hidden="true">◇</span>
  if (role === 'reviewer') return <span aria-hidden="true">✓</span>
  return <span aria-hidden="true">›_</span>
}

interface PlanningAgentInspectorProps {
  agent: PhaseAgent
  messages: PlanningMessage[]
  confirmed: boolean
  projectName: string
  sessionId: string
  onClose: () => void
}

function PlanningAgentInspector({
  agent,
  messages,
  confirmed,
  projectName,
  sessionId,
  onClose,
}: PlanningAgentInspectorProps) {
  const [tab, setTab] = useState<InspectorTab>('history')
  const agentMessages = useMemo(
    () =>
      agent.role === 'work-item'
        ? []
        : messages
            .filter((message) => message.role === agent.role)
            .sort((left, right) => left.sequence - right.sequence),
    [agent.role, messages],
  )
  const latestRawSequence = useMemo(
    () => [...agentMessages].reverse().find((message) => message.has_raw_output)?.sequence,
    [agentMessages],
  )
  const [rawOutput, setRawOutput] = useState<string | null>(null)
  const [rawError, setRawError] = useState<string | null>(null)
  const [rawLoading, setRawLoading] = useState(false)

  useEffect(() => {
    setTab('history')
  }, [agent.role])

  useEffect(() => {
    if (tab !== 'raw' || latestRawSequence === undefined) return

    const controller = new AbortController()
    setRawOutput(null)
    setRawError(null)
    setRawLoading(true)
    fetchPlanningMessageRaw(projectName, sessionId, latestRawSequence, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setRawOutput(result.raw_output)
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setRawError(err instanceof Error ? err.message : 'Unknown error')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setRawLoading(false)
      })

    return () => controller.abort()
  }, [latestRawSequence, projectName, sessionId, tab])

  // TODO(redesign): planning API exposes no cost/token usage.
  const stats = [
    ['provider', agent.provider ?? ''],
    ['model', agent.model ?? ''],
    ['turns', String(agentMessages.length)],
    ['cost', '—'],
    ['tokens in', '—'],
    ['tokens out', '—'],
  ]
  const subtitle = [agent.role, agent.provider].filter(Boolean).join(' · ')

  return (
    <aside className="planning-agent-inspector" aria-label={`${agent.label} inspector`}>
      <header className="planning-inspector-header">
        <span className="planning-inspector-icon"><InspectorIcon role={agent.role} /></span>
        <div className="planning-inspector-identity">
          <h2>{agent.label}</h2>
          <p>{subtitle}</p>
        </div>
        <span className={`planning-agent-dot is-${inspectorStatus(agent.state)}`} aria-label={agent.state} />
        <button type="button" className="planning-inspector-close" onClick={onClose} aria-label="Close agent inspector">
          ✕
        </button>
      </header>

      <dl className="planning-inspector-stats">
        {stats.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>

      <div className="planning-inspector-tabs" role="tablist" aria-label="Agent inspector views">
        {([
          ['history', 'History'],
          ['raw', 'Raw output'],
          ['controls', 'Controls'],
        ] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? 'is-active' : undefined}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="planning-inspector-content" role="tabpanel">
        {tab === 'history' && (
          agentMessages.length === 0 ? (
            <p className="planning-inspector-empty">No turns recorded.</p>
          ) : (
            <ol className="planning-inspector-history">
              {agentMessages.map((entry, index) => {
                const isConfirmed = agent.role === 'clarifier' && confirmed && index === agentMessages.length - 1
                const approved = entry.approved === true
                return (
                  <li key={entry.sequence}>
                    <span className="planning-inspector-history-line" aria-hidden="true" />
                    <div>
                      <p className="planning-inspector-turn-label">Turn {index + 1} · {messageKind(entry)}</p>
                      {entry.text && <p className="planning-inspector-turn-text">{entry.text}</p>}
                      {(approved || isConfirmed) && (
                        <span className="planning-inspector-approved">{approved ? 'approved' : 'confirmed'}</span>
                      )}
                    </div>
                  </li>
                )
              })}
            </ol>
          )
        )}

        {tab === 'raw' && (
          latestRawSequence === undefined ? (
            <p className="planning-inspector-empty">No raw output recorded.</p>
          ) : (
            <>
              {rawLoading && <p className="planning-inspector-empty">Loading raw output…</p>}
              {rawError && <p className="status status-error" role="alert">Failed to load raw output: {rawError}</p>}
              {rawOutput !== null && <pre className="planning-inspector-raw">{rawOutput}</pre>}
            </>
          )
        )}

        {tab === 'controls' && (
          // TODO(redesign): planning API does not support per-agent overrides after a session starts.
          <dl className="planning-inspector-controls">
            <div><dt>Provider</dt><dd>{agent.provider ?? ''}</dd></div>
            <div><dt>Model</dt><dd>{agent.model ?? ''}</dd></div>
            {agent.role === 'reviewer' && (
              <div><dt>Reasoning effort</dt><dd>{agent.reasoningEffort ?? ''}</dd></div>
            )}
          </dl>
        )}
      </div>
    </aside>
  )
}

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
function phaseTab(status: PlanningStatus): TabId {
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

function sessionStatusLine(session: PlanningSession): string {
  if (session.feature_status === 'building') {
    return `building · ${session.review_turn}/${session.max_review_turns}`
  }
  if (session.status === 'under_review') return `under review · rev ${session.plan_revision}`
  if (session.status === 'plan_ready') return 'plan ready'
  if (session.status === 'planning') return `planning · rev ${session.plan_revision}`
  if (session.status === 'review_limit_reached') return 'review limit reached'
  if (session.status === 'awaiting_confirmation') return 'awaiting confirmation'
  return session.status
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
