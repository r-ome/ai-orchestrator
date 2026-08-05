import { useEffect, useState, type ReactNode } from 'react'

interface ConfirmDialogProps {
  title: string
  /** Explains exactly what the action destroys. */
  children: ReactNode
  /** The user must type this string before the confirm button enables. */
  confirmPhrase: string
  confirmLabel: string
  busy?: boolean
  error?: string | null
  onConfirm: () => void
  onCancel: () => void
}

function ConfirmDialog({
  title,
  children,
  confirmPhrase,
  confirmLabel,
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState('')
  const matches = typed === confirmPhrase

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [busy, onCancel])

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="dialog"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
      >
        <h2>{title}</h2>
        <div className="dialog-body">{children}</div>

        <label className="dialog-field">
          Type <code>{confirmPhrase}</code> to confirm
          <input
            type="text"
            value={typed}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            onChange={(event) => setTyped(event.target.value)}
          />
        </label>

        {error && (
          <p className="status status-error" role="alert">
            {error}
          </p>
        )}

        <div className="dialog-actions">
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="danger"
            onClick={onConfirm}
            disabled={!matches || busy}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

export default ConfirmDialog
