import { useMemo, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CREDENTIAL_PROFILE_MAX_LENGTH,
  CREDENTIAL_PROFILE_PATTERN,
  fetchAgentProviders,
  fetchAgents,
  replaceAgent,
  stopAgent,
  summonAgent,
  type AgentProvider,
  type CodingAgent,
} from '../api/agents'
import ConfirmDialog from './ConfirmDialog'
import ContainerStatusBadge from './ContainerStatusBadge'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

interface ProjectAgentsSectionProps {
  projectName: string
  /** False while the project copy is still running; the backend rejects an
   *  agent for a project that is not ready. */
  projectReady: boolean
  summonOpen: boolean
  onSummonOpen: () => void
  onSummonClose: () => void
}

function ProjectAgentsSection({
  projectName,
  projectReady,
  summonOpen,
  onSummonOpen,
  onSummonClose,
}: ProjectAgentsSectionProps) {
  const navigate = useNavigate()
  const { data, loading, error, reload } = useApiResource(fetchAgents)
  const providers = useApiResource(fetchAgentProviders)

  const [provider, setProvider] = useState<AgentProvider>('claude')
  const [credentialProfile, setCredentialProfile] = useState('default')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const [pending, setPending] = useState<CodingAgent | null>(null)
  const [pendingReplace, setPendingReplace] = useState<CodingAgent | null>(null)
  const [stopBusy, setStopBusy] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)

  // The backend lists every managed agent, so narrow it to this project.
  const agents = useMemo(
    () => data?.agents.filter((agent) => agent.project_name === projectName) ?? [],
    [data, projectName],
  )
  const providerList = providers.data?.providers ?? []
  const selectedProvider = providerList.find(
    (details) => details.provider === provider,
  )
  const activeAgent = agents.find((agent) =>
    ['created', 'running', 'restarting', 'paused'].includes(agent.status),
  )

  const profileValid = CREDENTIAL_PROFILE_PATTERN.test(credentialProfile)
  const canSubmit = projectReady && profileValid && !busy && !activeAgent

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    setBusy(true)
    setFormError(null)
    try {
      const agent = await summonAgent(projectName, provider, credentialProfile)
      navigate(
        `/projects/${encodeURIComponent(projectName)}/agents/${encodeURIComponent(agent.id)}`,
      )
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error')
      setBusy(false)
    }
  }

  const confirmStop = async (agent: CodingAgent) => {
    setStopBusy(true)
    setStopError(null)
    try {
      await stopAgent(agent.id)
      setPending(null)
      reload()
    } catch (err) {
      setStopError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setStopBusy(false)
    }
  }

  const confirmReplace = async (agent: CodingAgent) => {
    setStopBusy(true)
    setStopError(null)
    try {
      const replacement = await replaceAgent(
        agent.id,
        provider,
        credentialProfile,
      )
      setPendingReplace(null)
      navigate(
        `/projects/${encodeURIComponent(projectName)}/agents/${encodeURIComponent(replacement.id)}`,
      )
    } catch (err) {
      setStopError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setStopBusy(false)
    }
  }

  return (
    <>
      <section id="coding-agents" className="card">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Coding agents</h2>
            {!error && !loading && (
              <span className="pill">{agents.length}</span>
            )}
          </div>
          <div className="button-row">
            <button
              type="button"
              className="primary small"
              onClick={onSummonOpen}
              disabled={!projectReady || loading || Boolean(activeAgent)}
            >
              Summon agent
            </button>
            <button
              type="button"
              className="small"
              onClick={reload}
              disabled={loading}
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </div>
        </div>

        <div className="card-body">
          {!projectReady && (
            <p className="status">
              This project is not ready yet. Wait for the copy to finish
              before summoning an agent.
            </p>
          )}

          {activeAgent && (
            <p className="status">
              This sandbox already has an active coding agent. Stop it or
              explicitly replace it. Sandbox files remain in place during
              replacement.
            </p>
          )}

          {credentialProfile !== '' && !profileValid && (
            <p className="status status-error">
              Profile must start with a letter or digit, then letters,
              digits, dots, underscores, or hyphens.
            </p>
          )}

          {providers.error && (
            <p className="status status-error" role="alert">
              Failed to load providers: {providers.error}
            </p>
          )}

          {formError && (
            <p className="status status-error" role="alert">
              {formError}
            </p>
          )}

          {error && (
            <p className="status status-error" role="alert">
              Failed to load agents: {error}
            </p>
          )}

          {!error && loading && <p className="status">Loading agents…</p>}

          {!error && !loading && agents.length === 0 && (
            <p className="status">No agents running for this project.</p>
          )}
        </div>

        {!error && !loading && agents.length > 0 && (
          <div className="table-wrapper">
            <table className="chrome-table">
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Provider</th>
                  <th>Profile</th>
                  <th>Status</th>
                  <th>Started</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {agents.map((agent) => (
                  <tr key={agent.id}>
                    <td className="mono">{agent.name}</td>
                    <td>{agent.provider}</td>
                    <td className="mono">{agent.credential_profile}</td>
                    <td>
                      <ContainerStatusBadge status={agent.status} />
                    </td>
                    <td title={formatTimestamp(agent.created_at)}>
                      {formatRelativeTime(agent.created_at)}
                    </td>
                    <td>
                      <div className="button-row">
                        <button
                          type="button"
                          className="small"
                          onClick={() =>
                            navigate(
                              `/projects/${encodeURIComponent(projectName)}/agents/${encodeURIComponent(agent.id)}`,
                            )
                          }
                        >
                          Open
                        </button>
                        <button
                          type="button"
                          className="danger small"
                          onClick={() => {
                            setPending(agent)
                            setStopError(null)
                          }}
                        >
                          Stop
                        </button>
                        <button
                          type="button"
                          className="small"
                          disabled={!profileValid}
                          onClick={() => {
                            setPendingReplace(agent)
                            setStopError(null)
                          }}
                        >
                          Replace
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {summonOpen && (
        <div className="dialog-backdrop" role="presentation">
          <form
            className="dialog summon-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="summon-agent-title"
            onSubmit={submit}
          >
            <div className="dialog-header">
              <h2 id="summon-agent-title">Summon coding agent</h2>
              <button
                type="button"
                className="dialog-close"
                aria-label="Close summon dialog"
                onClick={onSummonClose}
                disabled={busy}
              >
                ×
              </button>
            </div>
            <div className="dialog-body">
              <p className="status">
                Start an agent for <span className="mono">{projectName}</span>.
                It mounts the project at <span className="mono">/workspace</span>.
              </p>
              <label className="dialog-field">
                Provider
                <select
                  value={provider}
                  onChange={(event) =>
                    setProvider(event.target.value as AgentProvider)
                  }
                  disabled={busy || providers.loading}
                >
                  {providerList.map((details) => (
                    <option key={details.provider} value={details.provider}>
                      {details.provider}
                    </option>
                  ))}
                </select>
              </label>
              <label className="dialog-field">
                Credential profile
                <input
                  type="text"
                  value={credentialProfile}
                  placeholder="default"
                  maxLength={CREDENTIAL_PROFILE_MAX_LENGTH}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => setCredentialProfile(event.target.value)}
                  disabled={busy}
                />
              </label>
              {selectedProvider && (
                <p className="status summon-provider-note">
                  Image: <span className="mono">{selectedProvider.image}</span>, command:{' '}
                  <span className="mono">{selectedProvider.command.join(' ')}</span>
                </p>
              )}
              {credentialProfile !== '' && !profileValid && (
                <p className="status status-error">Profile name is not valid.</p>
              )}
              {providers.error && (
                <p className="status status-error" role="alert">
                  Failed to load providers: {providers.error}
                </p>
              )}
              {formError && (
                <p className="status status-error" role="alert">
                  {formError}
                </p>
              )}
            </div>
            <div className="dialog-actions">
              <button type="button" onClick={onSummonClose} disabled={busy}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={!canSubmit}>
                {busy ? 'Summoning…' : 'Summon agent'}
              </button>
            </div>
          </form>
        </div>
      )}

      {pending && (
        <ConfirmDialog
          title="Stop this agent?"
          confirmPhrase={pending.name}
          confirmLabel="Stop agent"
          busy={stopBusy}
          error={stopError}
          onCancel={() => {
            setPending(null)
            setStopError(null)
          }}
          onConfirm={() => confirmStop(pending)}
        >
          <p>
            This stops <strong>{pending.name}</strong> and removes the
            container. Files it wrote under{' '}
            <span className="mono">/workspace</span> stay in the project volume{' '}
            <span className="mono">{pending.project_volume}</span>, and the login
            stays in <span className="mono">{pending.credential_volume}</span>.
            Anything else the agent held in memory is lost.
          </p>
        </ConfirmDialog>
      )}


      {pendingReplace && (
        <ConfirmDialog
          title="Replace this agent?"
          confirmPhrase={pendingReplace.name}
          confirmLabel="Replace agent"
          busy={stopBusy}
          error={stopError}
          onCancel={() => {
            setPendingReplace(null)
            setStopError(null)
          }}
          onConfirm={() => confirmReplace(pendingReplace)}
        >
          <p>
            This stops <strong>{pendingReplace.name}</strong> and starts a new{' '}
            <strong>{provider}</strong> agent. Sandbox files and provider login
            volumes remain. Commands still running inside the current container
            stop immediately.
          </p>
        </ConfirmDialog>
      )}
    </>
  )
}

export default ProjectAgentsSection
