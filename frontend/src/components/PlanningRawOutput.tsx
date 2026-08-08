import { useEffect, useRef, useState, type SyntheticEvent } from 'react'
import type { AgentProvider } from '../api/agents'
import { fetchPlanningMessageRaw } from '../api/planning'

interface PlanningRawOutputProps {
  projectName: string
  sessionId: string
  sequence: number
  provider: AgentProvider
  /** The model recorded for this turn. Older turns have none. */
  model?: string
}

/**
 * Lazily loads one turn's raw container log into a disclosure.
 *
 * The log sits behind its own endpoint instead of riding along in the session
 * payload, which the page re-polls every two seconds while a session runs. A
 * closed disclosure therefore costs one boolean, not a container log.
 */
function PlanningRawOutput({
  projectName,
  sessionId,
  sequence,
  provider,
  model,
}: PlanningRawOutputProps) {
  const [raw, setRaw] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => () => controllerRef.current?.abort(), [])

  const onToggle = (event: SyntheticEvent<HTMLDetailsElement>) => {
    // Fetch on first open only. A reopen reuses what is already here, because
    // the log of a finished turn never changes.
    if (!event.currentTarget.open || raw !== null || loading) return

    const controller = new AbortController()
    controllerRef.current = controller
    setLoading(true)
    setError(null)
    fetchPlanningMessageRaw(projectName, sessionId, sequence, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setRaw(result.raw_output)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Unknown error')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
  }

  return (
    <details className="turn-raw" onToggle={onToggle}>
      <summary className="terminal-summary">
        <span className="collapsible-caret" aria-hidden="true" />
        <span className="terminal-lights" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="terminal-summary-label">Raw agent output</span>
        <span className="terminal-summary-meta">
          {provider}
          {model && (
            <>
              <span aria-hidden="true"> · </span>
              <span className="terminal-summary-model">{model}</span>
            </>
          )}
        </span>
      </summary>
      <div className="terminal-well">
        {provider === 'claude' && (
          <p className="terminal-note">
            # the clarifier, planner and reviewer run Claude with
            {' --output-format json'}, so this log is the result envelope rather
            than a reasoning trace
          </p>
        )}
        {loading && <p className="terminal-note">Loading raw output…</p>}
        {error && (
          <p className="terminal-note terminal-note-error" role="alert">
            Failed to load raw output: {error}
          </p>
        )}
        {raw !== null && (
          <pre className="terminal-stream">
            {raw || 'This turn recorded no output.'}
          </pre>
        )}
      </div>
    </details>
  )
}

export default PlanningRawOutput
