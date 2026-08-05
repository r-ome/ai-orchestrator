import { useState, type FormEvent, type ReactNode } from 'react'
import { formatBytes } from '../utils/format'

export interface FileDetails {
  path: string
  name: string
  size_bytes: number
  mode: string
  modified_at: string
  link_target: string
  encoding: string
  content: string
}

export interface FileReadResult {
  file: FileDetails
  /** Path as resolved inside the container. */
  resolvedPath: string
  /** Container the read went through. */
  via: string
}

interface FileReaderProps {
  onRead: (path: string) => Promise<FileReadResult>
  placeholder: string
  hint: string
  /** Extra controls rendered inside the form, e.g. a container picker. */
  controls?: ReactNode
}

/** Reads one file and renders it, or explains why it could not. */
function FileReader({ onRead, placeholder, hint, controls }: FileReaderProps) {
  const [path, setPath] = useState('')
  const [result, setResult] = useState<FileReadResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!path.trim()) return

    setLoading(true)
    setError(null)
    setResult(null)
    try {
      setResult(await onRead(path.trim()))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <form className="file-form" onSubmit={submit}>
        <label>
          File path
          <input
            type="text"
            value={path}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setPath(event.target.value)}
          />
        </label>

        {controls}

        <button
          type="submit"
          className="primary"
          disabled={loading || !path.trim()}
        >
          {loading ? 'Reading…' : 'Read file'}
        </button>
      </form>

      <p className="status">{hint}</p>

      {error && (
        <p className="status status-error" role="alert">
          {error}
        </p>
      )}

      {result && (
        <>
          <p className="status">
            <span className="mono">{result.resolvedPath}</span> ·{' '}
            {formatBytes(result.file.size_bytes)} · mode {result.file.mode} ·{' '}
            {result.file.encoding} · via {result.via}
          </p>
          {result.file.encoding === 'base64' ? (
            <p className="status">
              This file is not UTF-8 text. The API returned base64, which is not
              rendered here.
            </p>
          ) : (
            <pre className="file-content">{result.file.content}</pre>
          )}
        </>
      )}
    </>
  )
}

export default FileReader
