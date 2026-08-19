import { useEffect, useMemo, useState } from 'react'
import { fetchPlanningMessageRaw, type PlanningMessage } from '../api/planning'
import type { PhaseAgent } from './planningAgentInspectorModel'

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

export function PlanningAgentInspector({
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
