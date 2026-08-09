import { useEffect, useRef, useState } from 'react'
import {
  turnEventsUrl,
  type TurnKind,
  type TurnMessage,
  type TurnProgress,
} from '../api/delegation'
import { appendLines, summarise } from '../utils/turnOutput'

/**
 * Steps that mean the turn is over. `awaiting_decision` is a work-item run
 * whose turn finished but whose row stays running until a person accepts or
 * rejects it. Mirrors `TERMINAL_STEPS` in `app/turns/locators.py`.
 */
const TERMINAL_STEPS = new Set(['settled', 'awaiting_decision', 'failed'])

interface TurnConsoleProps {
  projectName: string
  sessionId: string
  kind: TurnKind
  /** The claimed row's id. A null id means nothing is running to watch. */
  jobId: string | null
  title: string
  /** Called once the turn reports a terminal step, so the page can reload. */
  onFinished?: () => void
}

/**
 * Live view of one background turn: controller milestones plus the assigned
 * model's own output.
 *
 * The output is the container's `claude -p --output-format stream-json` feed,
 * which is one JSON object per line. Lines that parse are summarised down to
 * the part a reader can act on; anything else is shown verbatim, because a
 * turn that is failing rarely fails in the documented format.
 */
function TurnConsole({
  projectName,
  sessionId,
  kind,
  jobId,
  title,
  onFinished,
}: TurnConsoleProps) {
  const [progress, setProgress] = useState<TurnProgress[]>([])
  const [lines, setLines] = useState<string[]>([])
  const [connected, setConnected] = useState(false)
  const [finished, setFinished] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const outputRef = useRef<HTMLPreElement>(null)
  // Held in a ref so reconnecting does not depend on the callback identity,
  // which would otherwise tear the socket down on every parent render.
  const finishedRef = useRef(onFinished)
  finishedRef.current = onFinished

  useEffect(() => {
    if (!jobId) return
    setProgress([])
    setLines([])
    setFailed(null)
    setFinished(false)

    const socket = new WebSocket(turnEventsUrl(projectName, sessionId, kind, jobId))
    let carry = ''
    // Both ends close once the turn settles, and a close that crosses one from
    // the other side arrives as an abnormal 1006 that fires `error`. That is
    // not a failure worth showing: the turn already reported its outcome.
    let settled = false

    socket.onopen = () => setConnected(true)
    socket.onclose = () => setConnected(false)
    socket.onerror = () => {
      if (!settled) setFailed('Lost the connection to the turn stream')
    }
    socket.onmessage = (event) => {
      let message: TurnMessage
      try {
        message = JSON.parse(String(event.data)) as TurnMessage
      } catch {
        return
      }
      if (message.type === 'progress') {
        if (TERMINAL_STEPS.has(message.step)) {
          settled = true
          setFinished(true)
        }
        setProgress((current) => [...current, message])
        return
      }
      if (message.type === 'end') {
        settled = true
        setFinished(true)
        finishedRef.current?.()
        socket.close()
        return
      }
      // Docker hands back whatever the socket returned, so the last line of a
      // chunk is usually a fragment. Carry it into the next chunk.
      const chunk = carry + message.data
      const split = chunk.split('\n')
      carry = split.pop() ?? ''
      const rendered = split.flatMap(summarise)
      if (rendered.length > 0) {
        // Cap retained output: a long turn emits tens of thousands of lines,
        // and the DOM is the thing that gives out first.
        setLines((current) => appendLines(current, rendered).slice(-500))
      }
    }

    return () => {
      // Unmounting is not a failure either, so suppress the error a close
      // mid-handshake would otherwise raise.
      settled = true
      socket.close()
      setConnected(false)
    }
  }, [projectName, sessionId, kind, jobId])

  useEffect(() => {
    const node = outputRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [lines])

  if (!jobId) return null

  return (
    <div className="turn-console">
      <div className="turn-console-header">
        <span className="section-heading">{title}</span>
        <span className={`pill ${connected ? 'warn' : finished ? 'ok' : 'muted'}`}>
          {connected ? 'streaming' : finished ? 'finished' : 'not connected'}
        </span>
      </div>

      {failed && <p className="status status-error">{failed}</p>}

      <ol className="turn-progress" aria-live="polite">
        {progress.length === 0 ? (
          <li className="status">Waiting for the controller…</li>
        ) : (
          progress.map((entry) => (
            <li key={entry.id} className={entry.level === 'error' ? 'status-error' : ''}>
              <span className="mono turn-step">{entry.step}</span>
              <span>{entry.message}</span>
            </li>
          ))
        )}
      </ol>

      <pre className="file-content turn-output" ref={outputRef}>
        {lines.length === 0 ? 'No output from the model yet.' : lines.join('\n')}
      </pre>
    </div>
  )
}

export default TurnConsole
