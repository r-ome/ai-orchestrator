import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { fetchAgent } from '../api/agents'
import AgentTerminal, { type TerminalPhase } from '../components/AgentTerminal'
import { useApiResource } from '../hooks/useApiResource'
import { formatTimestamp } from '../utils/format'

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
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to={projectPath}>← {projectName}</Link>
          </p>
          <h1>{data ? data.name : 'Agent terminal'}</h1>
        </div>
        <div className="page-header-actions">
          <span className={`agent-phase agent-phase-${phase}`}>
            {phase === 'connecting' && 'Connecting…'}
            {phase === 'live' && 'Connected'}
            {phase === 'closed' && 'Disconnected'}
          </span>
          {phase === 'closed' && data && (
            <button
              type="button"
              className="primary"
              onClick={() => {
                setTerminalError(null)
                setPhase('connecting')
                setTerminalGeneration((generation) => generation + 1)
              }}
            >
              Reconnect
            </button>
          )}
          <button type="button" onClick={() => navigate(projectPath)}>
            Back to project
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
          <p className="status">
            <span className="mono">{data.provider}</span> · profile{' '}
            <span className="mono">{data.credential_profile}</span> · workspace{' '}
            <span className="mono">{data.workspace}</span> · started{' '}
            {formatTimestamp(data.created_at)}
          </p>

          <p className="status" role="status">
            Leaving this page detaches the terminal. The agent keeps running,
            and you can open this page again to reattach to the same session.
          </p>

          {terminalError && (
            <p className="status status-error" role="alert">
              {terminalError}
            </p>
          )}

          <AgentTerminal
            key={`${data.id}-${terminalGeneration}`}
            agent={data}
            onPhase={setPhase}
            onError={setTerminalError}
          />

          {phase === 'closed' && (
            <p className="status">
              The terminal disconnected. The agent container and its session
              are still running. Select Reconnect, or{' '}
              <Link to={projectPath}>return to {data.project_name}</Link>.
            </p>
          )}
        </>
      )}
    </section>
  )
}

export default AgentTerminalPage
