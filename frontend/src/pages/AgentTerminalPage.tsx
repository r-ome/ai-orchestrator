import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { fetchAgent } from '../api/agents'
import AgentTerminal, { type TerminalPhase } from '../components/AgentTerminal'
import { useApiResource } from '../hooks/useApiResource'

function AgentTerminalPage() {
  const { projectName = '', agentId = '' } = useParams()
  const navigate = useNavigate()
  const projectPath = `/projects/${encodeURIComponent(projectName)}`
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchAgent(agentId, signal),
    [agentId],
  )
  const { data, loading, error } = useApiResource(fetcher, [agentId])

  const [phase, setPhase] = useState<TerminalPhase>('connecting')
  const [terminalError, setTerminalError] = useState<string | null>(null)
  const [terminalGeneration, setTerminalGeneration] = useState(0)

  return (
    <section className="agent-page">
      <header className="agent-terminal-header">
        <Link className="agent-terminal-back" to={projectPath}>
          ← {projectName}
        </Link>
        <span className="agent-terminal-divider" aria-hidden="true" />
        <span className="agent-terminal-dot" aria-hidden="true" />
        <div className="agent-terminal-identity">
          <div className="agent-terminal-name">
            {data ? data.name : 'Agent terminal'}
          </div>
          {data && (
            <div className="agent-terminal-meta">
              {data.provider} · profile {data.credential_profile} · workspace{' '}
              {data.workspace}
            </div>
          )}
        </div>
        <div className="agent-terminal-controls">
          <span className={`agent-terminal-status agent-phase-${phase}`}>
            <span className="agent-terminal-status-dot" aria-hidden="true" />
            {phase === 'connecting' && 'Connecting…'}
            {phase === 'live' && 'Connected'}
            {phase === 'closed' && 'Disconnected'}
          </span>
          <button
            type="button"
            onClick={() => {
              setTerminalError(null)
              setPhase('connecting')
              setTerminalGeneration((generation) => generation + 1)
            }}
          >
            ↻ Reconnect
          </button>
          <button type="button" onClick={() => navigate(projectPath)}>
            ⤡ Restore
          </button>
        </div>
      </header>

      {error && (
        <p className="status status-error" role="alert">
          Failed to load agent: {error}
        </p>
      )}

      {!error && loading && <p className="status">Loading agent…</p>}

      {data && (
        <>
          {terminalError && (
            <p className="agent-terminal-error status-error" role="alert">
              {terminalError}
            </p>
          )}

          <AgentTerminal
            key={`${data.id}-${terminalGeneration}`}
            agent={data}
            onPhase={setPhase}
            onError={setTerminalError}
          />

          <footer className="agent-terminal-footer">
            <span className="agent-terminal-live">
              <span aria-hidden="true" /> live
            </span>
            <span>ws · agent socket · binary frames</span>
            <span className="agent-terminal-footer-note">
              Leaving detaches the terminal — the agent keeps running.
            </span>
          </footer>
        </>
      )}
    </section>
  )
}

export default AgentTerminalPage
