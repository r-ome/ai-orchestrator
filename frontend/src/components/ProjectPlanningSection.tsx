import {
  useCallback,
  useMemo,
  useState,
  type FormEvent,
} from 'react'
import { Link, useNavigate } from 'react-router-dom'
import type { AgentProvider } from '../api/agents'
import {
  createPlanningSession,
  fetchPlanningDefaults,
  fetchPlanningSessions,
  isPlanningTerminal,
  type CreatePlanningSessionBody,
  type PlanningDefaults,
} from '../api/planning'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'
import FeatureStatusBadge from './FeatureStatusBadge'

interface ProjectPlanningSectionProps {
  projectName: string
  projectReady: boolean
  /** What this section is planning against, in the operator's words. */
  subject?: string
  /** Route prefix for this section's own pages, without a trailing slash. */
  basePath: string
}

const PROVIDERS: AgentProvider[] = ['claude', 'codex']

interface PlanningOverrides {
  clarifierProvider: AgentProvider
  plannerProvider: AgentProvider
  reviewerProvider: AgentProvider
  clarifierModel: string
  plannerModel: string
  reviewerModel: string
  reviewerReasoningEffort: string
  /** Kept as text so an emptied field stays empty while the operator retypes. */
  maxReviewTurns: string
}

function defaultModel(provider: AgentProvider, defaults: PlanningDefaults): string {
  return provider === 'claude' ? defaults.claude_model : defaults.codex_model
}

function defaultOverrides(defaults: PlanningDefaults): PlanningOverrides {
  return {
    clarifierProvider: defaults.clarifier_provider,
    plannerProvider: defaults.planner_provider,
    reviewerProvider: defaults.reviewer_provider,
    clarifierModel: defaultModel(defaults.clarifier_provider, defaults),
    plannerModel: defaultModel(defaults.planner_provider, defaults),
    reviewerModel: defaultModel(defaults.reviewer_provider, defaults),
    reviewerReasoningEffort: defaults.codex_reasoning_effort,
    maxReviewTurns: String(defaults.max_review_turns),
  }
}

function ProjectPlanningSection({
  projectName,
  projectReady,
  subject = 'workspace',
  basePath,
}: ProjectPlanningSectionProps) {
  const navigate = useNavigate()
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchPlanningSessions(projectName, signal),
    [projectName],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [projectName], {
    pollWhile: (data) =>
      data.sessions.some(
        (session) =>
          !isPlanningTerminal(session.status) ||
          session.feature_status === 'building' ||
          session.feature_status === 'in_review',
      ),
    intervalMs: 3_000,
  })
  const defaultsFetcher = useCallback(
    (signal: AbortSignal) => fetchPlanningDefaults(projectName, signal),
    [projectName],
  )
  const { data: defaults } = useApiResource(defaultsFetcher, [projectName])
  const [formOpen, setFormOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [request, setRequest] = useState('')
  const [overridesOpen, setOverridesOpen] = useState(false)
  const [overrides, setOverrides] = useState<PlanningOverrides | null>(null)
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const sessions = useMemo(
    () =>
      [...(data?.sessions ?? [])].sort((left, right) =>
        right.created_at.localeCompare(left.created_at),
      ),
    [data],
  )
  const canSubmit = projectReady && title.trim() !== '' && request.trim() !== '' && !busy

  const closeForm = () => {
    if (busy) return
    setFormOpen(false)
    setOverridesOpen(false)
    setOverrides(null)
    setFormError(null)
  }

  const toggleOverrides = () => {
    if (overridesOpen) {
      setOverridesOpen(false)
      setOverrides(null)
      return
    }
    if (!defaults) return
    setOverrides(defaultOverrides(defaults))
    setOverridesOpen(true)
  }

  const setProvider = (
    role: 'clarifier' | 'planner' | 'reviewer',
    provider: AgentProvider,
  ) => {
    if (!defaults) return
    setOverrides((current) => {
      if (!current) return current
      const model = defaultModel(provider, defaults)
      if (role === 'clarifier') {
        return { ...current, clarifierProvider: provider, clarifierModel: model }
      }
      if (role === 'planner') {
        return { ...current, plannerProvider: provider, plannerModel: model }
      }
      return { ...current, reviewerProvider: provider, reviewerModel: model }
    })
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    const body: CreatePlanningSessionBody = {
      title: title.trim(),
      request: request.trim(),
    }
    if (overridesOpen && overrides) {
      body.clarifier_provider = overrides.clarifierProvider
      body.planner_provider = overrides.plannerProvider
      body.reviewer_provider = overrides.reviewerProvider
      body.clarifier_model = overrides.clarifierModel
      body.planner_model = overrides.plannerModel
      body.reviewer_model = overrides.reviewerModel
      // Effort only reaches a codex reviewer, and an emptied limit field means
      // the operator wants whatever the backend already uses.
      if (overrides.reviewerProvider === 'codex') {
        body.reviewer_reasoning_effort = overrides.reviewerReasoningEffort
      }
      if (overrides.maxReviewTurns !== '') {
        body.max_review_turns = Number(overrides.maxReviewTurns)
      }
    }

    setBusy(true)
    setFormError(null)
    try {
      const session = await createPlanningSession(projectName, body)
      navigate(
        `${basePath}/plans/${encodeURIComponent(session.id)}`,
      )
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error')
      setBusy(false)
    }
  }

  return (
    <>
      <section id="planning" className="card project-planning-section">
        <div className="card-header">
          <div className="card-header-title">
            <h2>Planning</h2>
            {!error && !loading && <span className="pill">{sessions.length}</span>}
          </div>
          <div className="button-row">
            <button
              type="button"
              className="primary small"
              onClick={() => {
                setFormError(null)
                setFormOpen(true)
              }}
              disabled={!projectReady}
            >
              Plan a feature
            </button>
            <button type="button" className="small" onClick={reload} disabled={loading}>
              {loading ? 'Loading…' : 'Refresh'}
            </button>
          </div>
        </div>

        <div className="card-body">
          {!projectReady && (
            <p className="status">
              This {subject} is not ready yet. Wait for it to finish before
              planning a feature.
            </p>
          )}

          {error && (
            <p className="status status-error" role="alert">
              Failed to load planning sessions: {error}
            </p>
          )}

          {!error && loading && <p className="status">Loading planning sessions…</p>}

          {!error && !loading && sessions.length === 0 && (
            <p className="status">
              Planning produces a reviewed plan and changes nothing in this{' '}
              {subject}.
            </p>
          )}
        </div>

        {!error && !loading && sessions.length > 0 && (
          <div className="table-wrapper">
            <table className="chrome-table">
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>Status</th>
                  <th>Providers</th>
                  <th>Review</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((session) => (
                  <tr key={session.id}>
                    <td>
                      <Link
                        to={`${basePath}/plans/${encodeURIComponent(session.id)}`}
                      >
                        {session.title}
                      </Link>
                    </td>
                    <td>
                      <FeatureStatusBadge status={session.feature_status} />
                    </td>
                    <td className="mono">
                      {session.clarifier_provider} / {session.planner_provider} /{' '}
                      {session.reviewer_provider}
                    </td>
                    <td>
                      {session.review_turn > 0
                        ? `${session.review_turn} of ${session.max_review_turns}`
                        : '—'}
                    </td>
                    <td title={formatTimestamp(session.created_at)}>
                      {formatRelativeTime(session.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {formOpen && (
        <div className="dialog-backdrop" role="presentation">
          <form
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="plan-feature-title"
            onSubmit={submit}
          >
            <div className="dialog-header">
              <h2 id="plan-feature-title">Plan a feature</h2>
              <button
                type="button"
                className="dialog-close"
                aria-label="Close planning dialog"
                onClick={closeForm}
                disabled={busy}
              >
                ×
              </button>
            </div>
            <div className="dialog-body">
              <label className="dialog-field">
                Title
                <input
                  type="text"
                  value={title}
                  maxLength={200}
                  autoComplete="off"
                  onChange={(event) => setTitle(event.target.value)}
                  disabled={busy}
                  required
                />
              </label>
              <label className="dialog-field">
                Feature request
                <textarea
                  value={request}
                  maxLength={8000}
                  rows={6}
                  onChange={(event) => setRequest(event.target.value)}
                  disabled={busy}
                  required
                />
              </label>
              <div className="button-row">
                <button
                  type="button"
                  onClick={toggleOverrides}
                  aria-expanded={overridesOpen}
                  aria-controls="planning-overrides"
                  disabled={busy || !defaults}
                >
                  Override
                </button>
              </div>
              {overridesOpen && overrides && defaults && (
                <div id="planning-overrides">
                  <section className="planning-override-row">
                    <h3>Clarifier</h3>
                    <div className="planning-override-fields">
                      <label className="dialog-field">
                        Provider
                        <select
                          value={overrides.clarifierProvider}
                          onChange={(event) =>
                            setProvider('clarifier', event.target.value as AgentProvider)
                          }
                          disabled={busy}
                        >
                          {PROVIDERS.map((provider) => (
                            <option key={provider} value={provider}>
                              {provider}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="dialog-field">
                        Model
                        <select
                          value={overrides.clarifierModel}
                          onChange={(event) =>
                            setOverrides((current) =>
                              current
                                ? { ...current, clarifierModel: event.target.value }
                                : current,
                            )
                          }
                          disabled={busy}
                        >
                          {(defaults.models_by_provider[overrides.clarifierProvider] ?? []).map(
                            (model) => (
                              <option key={model} value={model}>
                                {model}
                              </option>
                            ),
                          )}
                        </select>
                      </label>
                    </div>
                  </section>
                  <section className="planning-override-row">
                    <h3>Planner</h3>
                    <div className="planning-override-fields">
                      <label className="dialog-field">
                        Provider
                        <select
                          value={overrides.plannerProvider}
                          onChange={(event) =>
                            setProvider('planner', event.target.value as AgentProvider)
                          }
                          disabled={busy}
                        >
                          {PROVIDERS.map((provider) => (
                            <option key={provider} value={provider}>
                              {provider}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="dialog-field">
                        Model
                        <select
                          value={overrides.plannerModel}
                          onChange={(event) =>
                            setOverrides((current) =>
                              current ? { ...current, plannerModel: event.target.value } : current,
                            )
                          }
                          disabled={busy}
                        >
                          {(defaults.models_by_provider[overrides.plannerProvider] ?? []).map(
                            (model) => (
                              <option key={model} value={model}>
                                {model}
                              </option>
                            ),
                          )}
                        </select>
                      </label>
                    </div>
                  </section>
                  <section className="planning-override-row">
                    <h3>Plan reviewer</h3>
                    <div
                      className={
                        overrides.reviewerProvider === 'codex'
                          ? 'planning-override-fields planning-override-fields--with-reasoning'
                          : 'planning-override-fields'
                      }
                    >
                      <label className="dialog-field">
                        Provider
                        <select
                          value={overrides.reviewerProvider}
                          onChange={(event) =>
                            setProvider('reviewer', event.target.value as AgentProvider)
                          }
                          disabled={busy}
                        >
                          {PROVIDERS.map((provider) => (
                            <option key={provider} value={provider}>
                              {provider}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="dialog-field">
                        Model
                        <select
                          value={overrides.reviewerModel}
                          onChange={(event) =>
                            setOverrides((current) =>
                              current ? { ...current, reviewerModel: event.target.value } : current,
                            )
                          }
                          disabled={busy}
                        >
                          {(defaults.models_by_provider[overrides.reviewerProvider] ?? []).map(
                            (model) => (
                              <option key={model} value={model}>
                                {model}
                              </option>
                            ),
                          )}
                        </select>
                      </label>
                      {overrides.reviewerProvider === 'codex' && (
                        <label className="dialog-field">
                          Reasoning effort
                          <select
                            value={overrides.reviewerReasoningEffort}
                            onChange={(event) =>
                              setOverrides((current) =>
                                current
                                  ? { ...current, reviewerReasoningEffort: event.target.value }
                                  : current,
                              )
                            }
                            disabled={busy}
                          >
                            {defaults.reasoning_efforts.map((effort) => (
                              <option key={effort} value={effort}>
                                {effort}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                    </div>
                  </section>
                  <label className="dialog-field">
                    Review-round limit
                    <input
                      type="number"
                      min="1"
                      max="10"
                      value={overrides.maxReviewTurns}
                      onChange={(event) =>
                        setOverrides((current) =>
                          current
                            ? { ...current, maxReviewTurns: event.target.value }
                            : current,
                        )
                      }
                      disabled={busy}
                    />
                  </label>
                </div>
              )}
              {formError && (
                <p className="status status-error" role="alert">
                  {formError}
                </p>
              )}
            </div>
            <div className="dialog-actions">
              <button type="button" onClick={closeForm} disabled={busy}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={!canSubmit}>
                {busy ? 'Creating…' : 'Create planning session'}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  )
}

export default ProjectPlanningSection
