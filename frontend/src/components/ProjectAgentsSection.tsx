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
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

interface ProjectAgentsSectionProps {
  projectName: string
  /** False while the project copy is still running; the backend rejects an
   *  agent for a project that is not ready. */
  projectReady: boolean
}

function ProjectAgentsSection({
  projectName,
  projectReady,
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
      <div className="section-header">
        <h2>Agents</h2>
        <button
          type="button"
          className="small"
          onClick={reload}
          disabled={loading}
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      <form className="file-form" onSubmit={submit}>
        <label>
          Provider
          <select
            value={provider}
            onChange={(event) =>
              setProvider(event.target.value as AgentProvider)
            }
          >
            {providerList.map((details) => (
              <option key={details.provider} value={details.provider}>
                {details.provider}
              </option>
            ))}
          </select>
        </label>

        <label>
          Credential profile
          <input
            type="text"
            value={credentialProfile}
            placeholder="default"
            maxLength={CREDENTIAL_PROFILE_MAX_LENGTH}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setCredentialProfile(event.target.value)}
          />
        </label>

        <button type="submit" className="primary" disabled={!canSubmit}>
          {busy ? 'Summoning…' : 'Summon'}
        </button>
      </form>

      <p className="status">
        The agent mounts this project's volume read-write at
        <span className="mono"> /workspace</span>. Each profile is its own Docker
        volume holding that provider's login: the first agent on a new profile
        starts signed out, and later agents on the same profile reuse it.
        {selectedProvider && (
          <>
            {' '}
            Image: <span className="mono">{selectedProvider.image}</span>,
            command:{' '}
            <span className="mono">{selectedProvider.command.join(' ')}</span>.
          </>
        )}
      </p>

      {!projectReady && (
        <p className="status">
          This project is not ready yet. Wait for the copy to finish before
          summoning an agent.
        </p>
      )}

      {activeAgent && (
        <p className="status">
          This sandbox already has an active coding agent. Stop it or explicitly
          replace it. Sandbox files remain in place during replacement.
        </p>
      )}

      {credentialProfile !== '' && !profileValid && (
        <p className="status status-error">
          Profile must start with a letter or digit, then letters, digits, dots,
          underscores, or hyphens.
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

      {!error && !loading && agents.length > 0 && (
        <div className="table-wrapper">
          <table className="volumes-table">
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
                  <td>{agent.status}</td>
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
