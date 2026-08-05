import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAgents } from '../api/agents'
import AgentTerminal, { type TerminalPhase } from './AgentTerminal'
import { useApiResource } from '../hooks/useApiResource'

interface ProjectTerminalDockProps {
  projectName: string
}

const ACTIVE_STATUSES = ['created', 'running', 'restarting', 'paused']

function ProjectTerminalDock({ projectName }: ProjectTerminalDockProps) {
  const navigate = useNavigate()
  const { data, loading, error } = useApiResource(fetchAgents)
  const [open, setOpen] = useState(true)
  const [phase, setPhase] = useState<TerminalPhase>('connecting')
  const [terminalError, setTerminalError] = useState<string | null>(null)
  const [generation, setGeneration] = useState(0)

  const agent = data?.agents.find(
    (candidate) =>
      candidate.project_name === projectName &&
      ACTIVE_STATUSES.includes(candidate.status),
  )

  if (loading || error || !agent) return null

  const terminalPath = `/projects/${encodeURIComponent(projectName)}/agents/${encodeURIComponent(agent.id)}`

  return (
    <aside className={`terminal-dock${open ? ' terminal-dock-open' : ''}`}>
      {open ? (
        <>
          <header className="terminal-dock-header">
            <div className="terminal-dock-title">
              <span className="terminal-dock-dot" aria-hidden="true" />
              <div>
                <div className="terminal-dock-name">{agent.name}</div>
                <div className="terminal-dock-meta">
                  {agent.provider} · profile {agent.credential_profile} ·{' '}
                  {agent.workspace}
                </div>
              </div>
            </div>
            <div className="terminal-dock-actions">
              <button
                type="button"
                title="Maximize terminal"
                onClick={() => navigate(terminalPath)}
              >
                ⤢
              </button>
              <button
                type="button"
                title="Reconnect terminal"
                onClick={() => {
                  setTerminalError(null)
                  setPhase('connecting')
                  setGeneration((value) => value + 1)
                }}
              >
                ↻
              </button>
              <button
                type="button"
                title="Collapse terminal"
                onClick={() => setOpen(false)}
              >
                ✕
              </button>
            </div>
          </header>
          <div className="terminal-dock-body">
            <AgentTerminal
              key={`${agent.id}-${generation}`}
              agent={agent}
              onPhase={setPhase}
              onError={setTerminalError}
            />
          </div>
          <footer className="terminal-dock-footer">
            <span className={`agent-phase agent-phase-${phase}`}>
              {phase === 'connecting' && 'Connecting…'}
              {phase === 'live' && 'Connected'}
              {phase === 'closed' && 'Disconnected'}
            </span>
            <span>{terminalError ?? 'Leaving detaches the terminal; the agent keeps running.'}</span>
          </footer>
        </>
      ) : (
        <>
          <button
            type="button"
            className="terminal-dock-collapsed"
            title="Open terminal"
            onClick={() => setOpen(true)}
          >
            ⌥
          </button>
          <span className="terminal-dock-collapsed-dot" aria-label="Agent connected" />
          <div className="terminal-dock-collapsed-label">
            {agent.name} · Connected
          </div>
        </>
      )}
    </aside>
  )
}

export default ProjectTerminalDock
