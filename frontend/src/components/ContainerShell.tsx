import { useEffect, useRef } from 'react'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { containerShellSocketUrl } from '../api/containers'

/** `connecting` until the server's `ready` frame, then `live` until the socket
 *  closes for any reason. */
export type ShellPhase = 'connecting' | 'live' | 'closed'

interface ContainerShellProps {
  containerId: string
  onPhase: (phase: ShellPhase) => void
  onError: (detail: string | null) => void
}

/** Why these codes: the backend closes with 4404 when the container is gone,
 *  4409 when it is not running, and 4503 when Docker is down. */
const CLOSE_REASONS: Record<number, string> = {
  4404: 'This container no longer exists.',
  4409: 'This container is not running.',
  4503: 'Docker daemon is unavailable.',
}

const MAX_COLUMNS = 500
const MAX_ROWS = 300

/** An interactive shell in any running container. Each mount opens its own
 *  `docker exec`, so leaving the page ends that shell for good. */
function ContainerShell({ containerId, onPhase, onError }: ContainerShellProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  // Callbacks are read through a ref so a parent re-render never tears down
  // the socket and kills the shell mid-command.
  const handlersRef = useRef({ onPhase, onError })
  handlersRef.current = { onPhase, onError }

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    const { onPhase: phase, onError: fail } = handlersRef.current
    phase('connecting')
    fail(null)
    const theme = getComputedStyle(document.documentElement)

    const terminal = new Terminal({
      convertEol: false,
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 13,
      scrollback: 10_000,
      theme: {
        background: theme.getPropertyValue('--terminal-bg').trim(),
        foreground: theme.getPropertyValue('--terminal-text').trim(),
        cursor: theme.getPropertyValue('--terminal-text').trim(),
      },
    })
    const fitAddon = new FitAddon()
    terminal.loadAddon(fitAddon)
    terminal.open(host)
    fitAddon.fit()

    const encoder = new TextEncoder()
    let socket: WebSocket | null = null
    let cancelled = false

    const sendResize = () => {
      if (socket?.readyState !== WebSocket.OPEN) return
      const columns = Math.min(terminal.cols, MAX_COLUMNS)
      const rows = Math.min(terminal.rows, MAX_ROWS)
      socket.send(JSON.stringify({ type: 'resize', columns, rows }))
    }

    const observer = new ResizeObserver(() => {
      try {
        fitAddon.fit()
      } catch {
        // The host can be measured at zero size mid-layout; the next
        // observation fits it properly.
        return
      }
      sendResize()
    })
    observer.observe(host)

    const inputSubscription = terminal.onData((data) => {
      if (socket?.readyState !== WebSocket.OPEN) return
      socket.send(encoder.encode(data))
    })

    // StrictMode mounts, unmounts, then remounts synchronously in development.
    // Opening on a timer avoids a throwaway WebSocket during that first pass.
    const openTimer = window.setTimeout(() => {
      if (cancelled) return
      socket = new WebSocket(containerShellSocketUrl(containerId))
      socket.binaryType = 'arraybuffer'

      socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          terminal.write(new Uint8Array(event.data))
          return
        }
        if (typeof event.data !== 'string') return

        let control: { type?: string; detail?: string; exit_code?: number | null }
        try {
          control = JSON.parse(event.data)
        } catch {
          return
        }

        if (control.type === 'ready') {
          handlersRef.current.onPhase('live')
          sendResize()
          terminal.focus()
        } else if (control.type === 'exit') {
          const code = control.exit_code
          terminal.write(
            `\r\n\x1b[2m— shell exited${code === null || code === undefined ? '' : ` (code ${code})`} —\x1b[0m\r\n`,
          )
        } else if (control.type === 'error' && control.detail) {
          handlersRef.current.onError(control.detail)
        }
      }

      socket.onclose = (event) => {
        if (cancelled) return
        handlersRef.current.onPhase('closed')
        const reason = CLOSE_REASONS[event.code]
        if (reason) handlersRef.current.onError(reason)
        else if (event.code !== 1000) {
          handlersRef.current.onError(
            event.reason || `Shell connection closed (code ${event.code}).`,
          )
        }
        terminal.write('\r\n\x1b[2m— disconnected —\x1b[0m\r\n')
      }

      socket.onerror = () => {
        if (!cancelled) handlersRef.current.onError('Shell connection failed.')
      }
    }, 0)

    return () => {
      cancelled = true
      window.clearTimeout(openTimer)
      observer.disconnect()
      inputSubscription.dispose()
      if (socket) {
        socket.onclose = null
        socket.onerror = null
        socket.onmessage = null
        socket.close(1000)
      }
      terminal.dispose()
    }
    // The parent changes the component key to request a new shell.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerId])

  return <div className="agent-terminal container-shell" ref={hostRef} />
}

export default ContainerShell
