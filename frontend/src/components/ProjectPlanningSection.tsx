import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from 'react'
import { Link, useNavigate } from 'react-router-dom'
import type { AgentProvider } from '../api/agents'
import {
  createPlanningSession,
  fetchPlanningSessions,
  isPlanningTerminal,
  type CreatePlanningSessionBody,
} from '../api/planning'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'
import PlanningStatusBadge from './PlanningStatusBadge'

interface ProjectPlanningSectionProps {
  projectName: string
  projectReady: boolean
  /** What this section is planning against, in the operator's words. A managed
   *  sandbox and a legacy local copy are both "not a project", so the noun is
   *  supplied by the page rather than assumed here. */
  subject?: string
}

const PROVIDERS: AgentProvider[] = ['claude', 'codex']

function ProjectPlanningSection({
  projectName,
  projectReady,
  subject = 'workspace',
}: ProjectPlanningSectionProps) {
  const navigate = useNavigate()
  const fetcher = useCallback(
    (signal: AbortSignal) => fetchPlanningSessions(projectName, signal),
    [projectName],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [projectName])
  const [formOpen, setFormOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [request, setRequest] = useState('')
  const [clarifierProvider, setClarifierProvider] = useState<AgentProvider | ''>(
    '',
  )
  const [plannerProvider, setPlannerProvider] = useState<AgentProvider | ''>('')
  const [reviewerProvider, setReviewerProvider] = useState<AgentProvider | ''>(
    '',
  )
  const [maxReviewTurns, setMaxReviewTurns] = useState('')
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const sessions = useMemo(
    () =>
      [...(data?.sessions ?? [])].sort((left, right) =>
        right.created_at.localeCompare(left.created_at),
      ),
    [data],
  )
  const hasActiveSession = sessions.some((session) => !isPlanningTerminal(session.status))
  const canSubmit = projectReady && title.trim() !== '' && request.trim() !== '' && !busy

  useEffect(() => {
    if (!hasActiveSession) return

    const timer = window.setInterval(reload, 3_000)
    return () => window.clearInterval(timer)
  }, [hasActiveSession, reload])

  const closeForm = () => {
    if (busy) return
    setFormOpen(false)
    setFormError(null)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return

    const body: CreatePlanningSessionBody = {
      title: title.trim(),
      request: request.trim(),
    }
    if (clarifierProvider) body.clarifier_provider = clarifierProvider
    if (plannerProvider) body.planner_provider = plannerProvider
    if (reviewerProvider) body.reviewer_provider = reviewerProvider
    if (maxReviewTurns !== '') body.max_review_turns = Number(maxReviewTurns)

    setBusy(true)
    setFormError(null)
    try {
      const session = await createPlanningSession(projectName, body)
      navigate(
        `/projects/${encodeURIComponent(projectName)}/plans/${encodeURIComponent(session.id)}`,
      )
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error')
      setBusy(false)
    }
  }

  return (
    <>
      <section id="planning" className="card">
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
                        to={`/projects/${encodeURIComponent(projectName)}/plans/${encodeURIComponent(session.id)}`}
                      >
                        {session.title}
                      </Link>
                    </td>
                    <td>
                      <PlanningStatusBadge status={session.status} />
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
              <label className="dialog-field">
                Clarifier provider
                <select
                  value={clarifierProvider}
                  onChange={(event) =>
                    setClarifierProvider(event.target.value as AgentProvider | '')
                  }
                  disabled={busy}
                >
                  <option value="">Use default</option>
                  {PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider}
                    </option>
                  ))}
                </select>
              </label>
              <label className="dialog-field">
                Planner provider
                <select
                  value={plannerProvider}
                  onChange={(event) =>
                    setPlannerProvider(event.target.value as AgentProvider | '')
                  }
                  disabled={busy}
                >
                  <option value="">Use default</option>
                  {PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider}
                    </option>
                  ))}
                </select>
              </label>
              <label className="dialog-field">
                Plan reviewer provider
                <select
                  value={reviewerProvider}
                  onChange={(event) =>
                    setReviewerProvider(event.target.value as AgentProvider | '')
                  }
                  disabled={busy}
                >
                  <option value="">Use default</option>
                  {PROVIDERS.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider}
                    </option>
                  ))}
                </select>
              </label>
              <label className="dialog-field">
                Review-round limit
                <input
                  type="number"
                  min="1"
                  max="10"
                  value={maxReviewTurns}
                  placeholder="Use default"
                  onChange={(event) => setMaxReviewTurns(event.target.value)}
                  disabled={busy}
                />
              </label>
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
