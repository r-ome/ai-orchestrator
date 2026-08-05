import { useCallback, useEffect, useRef, useState } from 'react'
import {
  actOnPreview,
  fetchCurrentPreview,
  fetchPreviewCreationLogs,
  fetchPreviewLogs,
  inspectPreview,
  keepPreviewAlive,
  startPreview,
  stopPreview,
  type PreviewConfiguration,
  type PreviewDependencyService,
  type PreviewLogs,
  type PreviewMode,
  type PreviewNetworkAccess,
  type PreviewPersistence,
  type PreviewProposal,
  type PreviewRun,
  type PreviewRuntime,
  type PreviewSharing,
} from '../api/previews'
import { describeSharing } from '../utils/databaseSharing'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

interface ProjectPreviewSectionProps {
  projectName: string
  projectReady: boolean
}

function ProjectPreviewSection({
  projectName,
  projectReady,
}: ProjectPreviewSectionProps) {
  const [current, setCurrent] = useState<PreviewRun | null>(null)
  const [proposal, setProposal] = useState<PreviewProposal | null>(null)
  const [config, setConfig] = useState<PreviewConfiguration | null>(null)
  const [proposalOpen, setProposalOpen] = useState(false)
  const [logs, setLogs] = useState<PreviewLogs | null>(null)
  const [creationProposalId, setCreationProposalId] = useState<string | null>(null)
  const [buildLogsOpen, setBuildLogsOpen] = useState(false)
  const [followRuntimeLogs, setFollowRuntimeLogs] = useState(false)
  const [approved, setApproved] = useState(false)
  const [configEdited, setConfigEdited] = useState(false)
  const [saveDefault, setSaveDefault] = useState(false)
  const [removeData, setRemoveData] = useState(true)
  const [confirmStop, setConfirmStop] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [clock, setClock] = useState(Date.now())
  const logOutputs = useRef<Record<string, HTMLPreElement | null>>({})

  const loadCurrent = useCallback(async () => {
    try {
      const preview = await fetchCurrentPreview(projectName)
      setCurrent(preview)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error'
      if (message === 'Sandbox has no active preview') {
        setCurrent(null)
      } else {
        setError(message)
      }
    }
  }, [projectName])

  useEffect(() => {
    void loadCurrent()
  }, [loadCurrent])

  useEffect(() => {
    if (!current?.expires_at) return
    const timer = window.setInterval(() => setClock(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [current?.expires_at])

  useEffect(() => {
    if (!creationProposalId) return
    let cancelled = false
    const refresh = async () => {
      try {
        const nextLogs = await fetchPreviewCreationLogs(
          projectName,
          creationProposalId,
        )
        if (!cancelled) setLogs(nextLogs)
      } catch {
        // The start request reports the authoritative error.
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 750)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [creationProposalId, projectName])

  useEffect(() => {
    if (!followRuntimeLogs || !current || creationProposalId) return
    let cancelled = false
    const refresh = async () => {
      try {
        const nextLogs = await fetchPreviewLogs(projectName)
        if (!cancelled) setLogs(nextLogs)
      } catch (err) {
        if (cancelled) return
        setFollowRuntimeLogs(false)
        setError(err instanceof Error ? err.message : 'Unknown error')
      }
    }
    const timer = window.setInterval(() => void refresh(), 2_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [creationProposalId, current, followRuntimeLogs, projectName])

  useEffect(() => {
    if (!followRuntimeLogs) return
    for (const output of Object.values(logOutputs.current)) {
      if (output) output.scrollTop = output.scrollHeight
    }
  }, [followRuntimeLogs, logs])

  const remainingSeconds = current?.expires_at
    ? Math.max(Math.ceil((Date.parse(current.expires_at) - clock) / 1_000), 0)
    : null
  const expiryWarning = remainingSeconds !== null && remainingSeconds <= 5 * 60

  const inspect = async () => {
    setLoading(true)
    setError(null)
    setNotice(null)
    setLogs(null)
    setCreationProposalId(null)
    setBuildLogsOpen(false)
    setFollowRuntimeLogs(false)
    try {
      const detected = await inspectPreview(projectName)
      setProposal(detected)
      setConfig(detected.config)
      setApproved(false)
      setConfigEdited(false)
      setProposalOpen(true)
      setNotice('Inspection completed. Review every setting before approval.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const launch = async () => {
    if (!proposal || !config) return
    if ((proposal.approval_required || configEdited) && !approved) return
    setLoading(true)
    setError(null)
    setNotice(null)
    const proposalId = proposal.id
    setProposalOpen(false)
    setBuildLogsOpen(true)
    setCreationProposalId(proposalId)
    setFollowRuntimeLogs(false)
    setLogs({
      proposal_id: proposalId,
      preview_id: '',
      status: 'waiting',
      events: [],
      logs: {},
    })
    try {
      const preview = await startPreview(
        projectName,
        proposal,
        config,
        current ? 'rebuild' : 'start',
        saveDefault,
      )
      setCurrent(preview)
      setProposal(null)
      setConfig(null)
      setApproved(false)
      setConfigEdited(false)
      setProposalOpen(false)
      setNotice(current ? 'Preview rebuilt.' : 'Preview started.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      try {
        setLogs(await fetchPreviewCreationLogs(projectName, proposalId))
      } catch {
        // Keep the latest successful polling result.
      }
      setCreationProposalId(null)
      setBuildLogsOpen(false)
      setLoading(false)
    }
  }

  const clearProposal = () => {
    setProposal(null)
    setConfig(null)
    setApproved(false)
    setConfigEdited(false)
    setProposalOpen(false)
  }

  const action = async (name: 'reuse' | 'restart') => {
    setLoading(true)
    setError(null)
    setNotice(null)
    try {
      const preview = await actOnPreview(projectName, name)
      setCurrent(preview)
      setNotice(name === 'reuse' ? 'Existing preview reused.' : 'Preview restarted.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const keepAlive = async () => {
    setLoading(true)
    setError(null)
    try {
      const preview = await keepPreviewAlive(
        projectName,
        config?.expiry_minutes ?? 30,
      )
      setCurrent(preview)
      setNotice('Preview expiry extended.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const showLogs = async () => {
    setLoading(true)
    setError(null)
    try {
      setLogs(await fetchPreviewLogs(projectName))
      setFollowRuntimeLogs(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const refreshLogs = async () => {
    setError(null)
    try {
      setLogs(await fetchPreviewLogs(projectName))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    }
  }

  const stop = async () => {
    setLoading(true)
    setError(null)
    try {
      await stopPreview(projectName, removeData)
      setCurrent(null)
      setLogs(null)
      setFollowRuntimeLogs(false)
      setConfirmStop(false)
      setNotice('Preview stopped.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  const update = <Key extends keyof PreviewConfiguration>(
    key: Key,
    value: PreviewConfiguration[Key],
  ) => {
    setConfig((previous) => (previous ? { ...previous, [key]: value } : previous))
    setApproved(false)
    setConfigEdited(true)
  }

  const updateDatabase = <Key extends keyof PreviewDependencyService>(
    key: Key,
    value: PreviewDependencyService[Key],
  ) => {
    setConfig((previous) => {
      const database = previous?.services.database
      if (!previous || !database) return previous
      return {
        ...previous,
        services: {
          ...previous.services,
          database: { ...database, [key]: value },
        },
      }
    })
    setApproved(false)
    setConfigEdited(true)
  }

  /** Sharing and its target move together: a target is meaningless without
   *  shared_data, and shared_data is invalid without a target. */
  const setDatabaseSharing = (sharing: PreviewSharing) => {
    setConfig((previous) => {
      const database = previous?.services.database
      if (!previous || !database) return previous
      const candidate = proposal?.share_candidates[0]
      return {
        ...previous,
        services: {
          ...previous.services,
          database: {
            ...database,
            sharing,
            share_target:
              sharing === 'shared_data'
                ? database.share_target || candidate?.sandbox_id || ''
                : '',
            image:
              sharing === 'shared_data' && candidate
                ? candidate.image
                : database.image,
          },
        },
      }
    })
    setApproved(false)
    setConfigEdited(true)
  }

  const setShareTarget = (sandboxId: string) => {
    setConfig((previous) => {
      const database = previous?.services.database
      if (!previous || !database) return previous
      const candidate = proposal?.share_candidates.find(
        (entry) => entry.sandbox_id === sandboxId,
      )
      return {
        ...previous,
        services: {
          ...previous.services,
          database: {
            ...database,
            share_target: sandboxId,
            // The backend rejects a mismatch, so follow the target's image.
            image: candidate ? candidate.image : database.image,
          },
        },
      }
    })
    setApproved(false)
    setConfigEdited(true)
  }

  const setDatabaseEnabled = (enabled: boolean) => {
    setConfig((previous) => {
      if (!previous) return previous
      if (!enabled) {
        const services = { ...previous.services }
        const environment = { ...previous.environment }
        delete services.database
        delete environment.DATABASE_URL
        return {
          ...previous,
          services,
          initialize: { commands: [] },
          environment,
        }
      }
      return {
        ...previous,
        services: {
          ...previous.services,
          database: {
            type: 'mysql',
            image: 'mysql:8.4',
            database: 'atc_preview',
            persistence: 'ephemeral',
            sharing: 'isolated',
            share_target: '',
          },
        },
        initialize: {
          commands: previous.initialize.commands.length
            ? previous.initialize.commands
            : ['npx prisma migrate deploy'],
        },
        environment: {
          ...previous.environment,
          DATABASE_URL: { from_service: 'database', from_secret: '' },
        },
      }
    })
    setApproved(false)
    setConfigEdited(true)
  }

  const updateInitializationCommands = (value: string) => {
    setConfig((previous) =>
      previous
        ? {
            ...previous,
            initialize: {
              commands: value
                .split('\n')
                .map((command) => command.trim())
                .filter(Boolean),
            },
          }
        : previous,
    )
    setApproved(false)
    setConfigEdited(true)
  }

  return (
    <section className="preview-section card">
      <div className="section-header">
        <h2>Preview</h2>
        <div className="button-row">
          {proposal && !proposalOpen && (
            <button
              type="button"
              className="small"
              onClick={() => setProposalOpen(true)}
              disabled={loading}
            >
              Review proposal
            </button>
          )}
          <button
            type="button"
            className="small"
            onClick={() => void inspect()}
            disabled={!projectReady || loading}
          >
            {loading ? 'Working…' : current ? 'Inspect for rebuild' : 'Inspect sandbox'}
          </button>
        </div>
      </div>

      <p className="status">
        Inspection suggests settings without executing project code. Starting or
        rebuilding requires explicit approval.
      </p>

      {!projectReady && (
        <p className="status">Wait for the sandbox copy to finish.</p>
      )}

      {error && (
        <p className="status status-error" role="alert">
          {error}
        </p>
      )}
      {notice && <p className="status status-ok">{notice}</p>}

      {current && (
        <div className="preview-current">
          <dl className="detail-grid">
            <dt>Status</dt>
            <dd>{current.status}</dd>
            <dt>Mode</dt>
            <dd>{current.mode}</dd>
            <dt>Service</dt>
            <dd>{current.selected_service || 'app'}</dd>
            <dt>Port</dt>
            <dd className="mono">
              127.0.0.1:{current.host_port} → {current.container_port}
            </dd>
            <dt>Runtime network</dt>
            <dd>{current.network_access}</dd>
            {current.database_sharing && (
              <>
                <dt>Database</dt>
                <dd>{describeSharing(current.database_sharing)}</dd>
              </>
            )}
            <dt>Expires</dt>
            <dd title={formatTimestamp(current.expires_at)}>
              {current.expires_at ? formatRelativeTime(current.expires_at) : 'Never'}
            </dd>
          </dl>

          <div className="button-row preview-actions">
            {current.url && (
              <a
                className="button-link primary"
                href={current.url}
                target="_blank"
                rel="noreferrer"
                onClick={() => void action('reuse')}
              >
                Open preview
              </a>
            )}
            <button type="button" onClick={() => void action('reuse')} disabled={loading}>
              Reuse
            </button>
            <button type="button" onClick={() => void action('restart')} disabled={loading}>
              Restart
            </button>
            <button type="button" onClick={() => void keepAlive()} disabled={loading}>
              Keep running
            </button>
            <button type="button" onClick={() => void showLogs()} disabled={loading}>
              View live logs
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => setConfirmStop(true)}
              disabled={loading}
            >
              Stop
            </button>
          </div>

          {remainingSeconds !== null && (
            <p className={`status ${expiryWarning ? 'status-warning' : ''}`}>
              {remainingSeconds === 0
                ? 'Preview expiry is due. The controller will stop it shortly.'
                : `Preview stops in ${Math.floor(remainingSeconds / 60)}:${String(
                    remainingSeconds % 60,
                  ).padStart(2, '0')}.`}
              {expiryWarning ? ' Use Keep running to extend it.' : ''}
            </p>
          )}

          {confirmStop && (
            <div className="inline-confirm">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={removeData}
                  onChange={(event) => setRemoveData(event.target.checked)}
                />
                Remove other nonpersistent preview data volumes. Ephemeral database
                data is always removed.
              </label>
              <div className="button-row">
                <button type="button" onClick={() => setConfirmStop(false)}>
                  Cancel
                </button>
                <button type="button" className="danger" onClick={() => void stop()}>
                  Confirm stop
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {proposal && config && proposalOpen && (
        <div className="dialog-backdrop preview-modal-backdrop">
          <section
            className="dialog preview-proposal-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="preview-proposal-title"
          >
            <div className="preview-modal-header">
              <div>
                <h2 id="preview-proposal-title">Rebuild preview stack</h2>
                <p className="status">Review the exact settings and protected-file changes before rebuilding.</p>
              </div>
              <button
                type="button"
                className="small ghost"
                aria-label="Close rebuild proposal"
                onClick={() => setProposalOpen(false)}
              >
                ✕
              </button>
            </div>
            <div className="preview-proposal">
          <h3>Proposed settings</h3>
          <p className="status">
            Detected {proposal.detected_runtime} in {proposal.detected_mode} mode.
            Confidence: {proposal.confidence}. Evidence:{' '}
            <span className="mono">{proposal.evidence.join(', ') || 'none'}</span>.
          </p>

          <div className="preview-form-grid">
            <label>
              Mode
              <select
                value={config.mode}
                onChange={(event) => update('mode', event.target.value as PreviewMode)}
              >
                <option value="native">Native</option>
                <option value="dockerfile">Dockerfile</option>
                <option value="compose">Compose</option>
                <option value="unknown">Unknown</option>
              </select>
            </label>

            <label>
              Runtime
              <select
                value={config.runtime}
                onChange={(event) =>
                  update('runtime', event.target.value as PreviewRuntime)
                }
              >
                <option value="static">Static HTML</option>
                <option value="vite">Vite</option>
                <option value="astro">Astro</option>
                <option value="nextjs">Next.js</option>
                <option value="fastapi">FastAPI</option>
                <option value="unknown">Unknown</option>
              </select>
            </label>

            {config.mode === 'native' && (
              <>
                <label>
                  Runtime image
                  <input value={config.image} onChange={(event) => update('image', event.target.value)} />
                </label>
                <label>
                  Install command
                  <input
                    value={config.install_command}
                    onChange={(event) => update('install_command', event.target.value)}
                  />
                </label>
                <label className="wide-field">
                  Start command
                  <input
                    value={config.start_command}
                    onChange={(event) => update('start_command', event.target.value)}
                  />
                </label>
                <div className="wide-field dependency-settings">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={Boolean(config.services.database)}
                      onChange={(event) => setDatabaseEnabled(event.target.checked)}
                    />
                    Use controller-managed MySQL
                  </label>
                  {config.services.database && (
                    <div className="preview-form-grid">
                      <label>
                        Database image
                        <input
                          value={config.services.database.image}
                          onChange={(event) =>
                            updateDatabase('image', event.target.value)
                          }
                        />
                      </label>
                      <label>
                        Database name
                        <input
                          value={config.services.database.database}
                          onChange={(event) =>
                            updateDatabase('database', event.target.value)
                          }
                        />
                      </label>
                      <label>
                        Database data
                        <select
                          value={config.services.database.persistence}
                          onChange={(event) =>
                            updateDatabase(
                              'persistence',
                              event.target.value as PreviewPersistence,
                            )
                          }
                        >
                          <option value="ephemeral">Ephemeral</option>
                          <option value="persistent">Persistent</option>
                        </select>
                      </label>
                      <label>
                        Database sharing
                        <select
                          value={config.services.database.sharing}
                          onChange={(event) =>
                            setDatabaseSharing(
                              event.target.value as PreviewSharing,
                            )
                          }
                        >
                          <option value="isolated">
                            Isolated — own server
                          </option>
                          <option value="shared_server">
                            Shared server — own schema
                          </option>
                          <option
                            value="shared_data"
                            disabled={proposal.share_candidates.length === 0}
                          >
                            Shared data — join another sandbox
                          </option>
                        </select>
                      </label>
                      {config.services.database.sharing === 'shared_data' && (
                        <label>
                          Join this sandbox's data
                          <select
                            value={config.services.database.share_target}
                            onChange={(event) =>
                              setShareTarget(event.target.value)
                            }
                          >
                            {proposal.share_candidates.map((candidate) => (
                              <option
                                key={candidate.sandbox_id}
                                value={candidate.sandbox_id}
                              >
                                {candidate.project_name} ({candidate.image},{' '}
                                {candidate.attached_sandboxes} guest(s))
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                      <label className="wide-field">
                        Initialization commands, one per line
                        <textarea
                          rows={3}
                          value={config.initialize.commands.join('\n')}
                          onChange={(event) =>
                            updateInitializationCommands(event.target.value)
                          }
                        />
                      </label>
                      {config.services.database.sharing === 'shared_server' && (
                        <p className="status wide-field">
                          One MySQL container serves every sandbox of this
                          project. This sandbox still gets its own schema, so
                          its migrations stay invisible to the others.
                        </p>
                      )}
                      {config.services.database.sharing === 'shared_data' && (
                        <p className="status status-warning wide-field">
                          This sandbox writes to another sandbox's data. Every
                          schema change it makes is visible to that sandbox and
                          to every other guest. Initialization commands are
                          skipped, because a guest must not migrate or seed data
                          it does not own.
                        </p>
                      )}
                      {proposal.share_candidates.length === 0 &&
                        config.services.database.sharing !== 'shared_data' && (
                          <p className="status wide-field">
                            No sibling sandbox of this project holds a database
                            yet, so there is nothing to join.
                          </p>
                        )}
                      <p className="status wide-field">
                        The controller generates credentials and injects{' '}
                        <span className="mono">DATABASE_URL</span>. It does not publish
                        MySQL to the host.
                      </p>
                    </div>
                  )}
                </div>
              </>
            )}

            {config.mode === 'dockerfile' && (
              <label>
                Dockerfile
                <input
                  value={config.dockerfile}
                  onChange={(event) => update('dockerfile', event.target.value)}
                />
              </label>
            )}

            {config.mode === 'compose' && (
              <>
                <label>
                  Compose file
                  <input
                    value={config.compose_file}
                    onChange={(event) => update('compose_file', event.target.value)}
                  />
                </label>
                <label>
                  Preview service
                  <select
                    value={config.selected_service}
                    onChange={(event) => update('selected_service', event.target.value)}
                  >
                    {proposal.available_services.map((service) => (
                      <option key={service} value={service}>
                        {service}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            )}

            <label>
              Container port
              <input
                type="number"
                min={1}
                max={65535}
                value={config.container_port}
                onChange={(event) => update('container_port', Number(event.target.value))}
              />
            </label>
            <label>
              Host port
              <input
                type="number"
                min={1}
                max={65535}
                placeholder="Automatic"
                value={config.host_port ?? ''}
                onChange={(event) =>
                  update('host_port', event.target.value ? Number(event.target.value) : null)
                }
              />
            </label>
            <label>
              Runtime network
              <select
                value={config.network_access}
                onChange={(event) =>
                  update(
                    'network_access',
                    event.target.value as PreviewNetworkAccess,
                  )
                }
              >
                <option value="isolated">Isolated</option>
                <option value="internet">Internet access</option>
              </select>
            </label>
            <label>
              Idle expiry
              <select
                value={config.expiry_minutes}
                onChange={(event) => update('expiry_minutes', Number(event.target.value))}
              >
                <option value={15}>15 minutes</option>
                <option value={30}>30 minutes</option>
                <option value={60}>1 hour</option>
                <option value={0}>No expiry</option>
              </select>
            </label>
          </div>

          {proposal.required_environment.length > 0 && (
            <>
              <h3>Environment</h3>
              <ul className="preview-environment-list">
                {proposal.required_environment.map((variable) => {
                  const configured = proposal.configured_environment.includes(variable)
                  return (
                    <li
                      key={variable}
                      className={configured ? 'status-ok' : 'status-warning'}
                    >
                      {configured ? '✓' : '⚠'}{' '}
                      <span className="mono">{variable}</span>
                    </li>
                  )
                })}
              </ul>
              {proposal.missing_environment.length > 0 && (
                <p className="status status-warning">
                  Missing:{' '}
                  <span className="mono">
                    {proposal.missing_environment.join(', ')}
                  </span>
                  . The preview will start without these. Set them in the
                  Preview secrets section above.
                </p>
              )}
            </>
          )}

          <h3>Protected runtime files</h3>
          {proposal.changes.length === 0 ? (
            <p className="status">No protected runtime files changed.</p>
          ) : (
            <div className="protected-changes">
              {proposal.changes.map((change) => (
                <details key={change.path} open>
                  <summary>
                    <span className="mono">{change.path}</span> — {change.change}
                  </summary>
                  <pre className="file-content">{change.diff || 'Content changed.'}</pre>
                </details>
              ))}
            </div>
          )}

          {!proposal.approval_required && !configEdited && (
            <p className="status status-ok">
              These settings and protected files match the latest approval.
            </p>
          )}

          {(proposal.approval_required || configEdited) && (
            <label className="checkbox-label approval-check">
              <input
                type="checkbox"
                checked={approved}
                onChange={(event) => setApproved(event.target.checked)}
              />
              I reviewed the settings and protected-file changes.
            </label>
          )}
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={saveDefault}
              onChange={(event) => setSaveDefault(event.target.checked)}
            />
            Save approved settings to <span className="mono">.agent/preview.yaml</span>
          </label>

          <div className="button-row proposal-actions">
            <button
              type="button"
              onClick={clearProposal}
            >
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              disabled={
                (proposal.approval_required || configEdited) && !approved
                  ? true
                  : loading
              }
              onClick={() => void launch()}
            >
              {current ? 'Approve and rebuild' : 'Approve and start'}
            </button>
          </div>
            </div>
          </section>
        </div>
      )}

      {logs && (!creationProposalId || buildLogsOpen) && (
        <div
          className={`preview-logs${creationProposalId ? ' preview-build-modal' : ''}`}
        >
          <div className={creationProposalId ? 'preview-build-surface' : undefined}>
          <div className="section-header">
            <h3>{creationProposalId ? 'Creating preview' : 'Live preview logs'}</h3>
            <div className="button-row">
              {creationProposalId && (
                <button
                  type="button"
                  className="small"
                  onClick={() => setBuildLogsOpen(false)}
                >
                  Run in background
                </button>
              )}
              {current && !creationProposalId && (
                <>
                  <button
                    type="button"
                    className="small"
                    onClick={() => setFollowRuntimeLogs((following) => !following)}
                  >
                    {followRuntimeLogs ? 'Pause' : 'Follow live'}
                  </button>
                  <button
                    type="button"
                    className="small"
                    onClick={() => void refreshLogs()}
                  >
                    Refresh
                  </button>
                </>
              )}
              <button
                type="button"
                className="small"
                onClick={() => {
                  setFollowRuntimeLogs(false)
                  setLogs(null)
                }}
                disabled={Boolean(creationProposalId)}
              >
                Close
              </button>
            </div>
          </div>
          <p className="status">
            Status: <span className="mono">{logs.status}</span>
          </p>
          {logs.events.length === 0 ? (
            <p className="status">Waiting for the preview controller…</p>
          ) : (
            <ol className="preview-progress-events" aria-live="polite">
              {logs.events.map((event) => (
                <li key={event.id} className={event.level === 'error' ? 'status-error' : ''}>
                  <span className="mono">{event.step}</span>
                  <span>{event.message}</span>
                  <time dateTime={event.created_at} title={formatTimestamp(event.created_at)}>
                    {formatRelativeTime(event.created_at)}
                  </time>
                </li>
              ))}
            </ol>
          )}
          {Object.entries(logs.logs).map(([container, output]) => (
            <details key={container} open>
              <summary className="mono">{container}</summary>
              <pre
                className="file-content"
                ref={(node) => {
                  logOutputs.current[container] = node
                }}
              >
                {output || 'No stdout or stderr output.'}
              </pre>
            </details>
          ))}
          </div>
        </div>
      )}
    </section>
  )
}

export default ProjectPreviewSection
