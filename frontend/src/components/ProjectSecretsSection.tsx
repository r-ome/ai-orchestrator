import { Fragment, useCallback, useState, type FormEvent } from 'react'
import {
  deleteProjectSecret,
  fetchProjectSecrets,
  importProjectSecrets,
  setProjectSecrets,
  SECRET_NAME_PATTERN,
  SECRET_VALUE_MAX_BYTES,
  type ProjectSecretName,
} from '../api/previews'
import ConfirmDialog from './ConfirmDialog'
import { useApiResource } from '../hooks/useApiResource'
import { formatRelativeTime, formatTimestamp } from '../utils/format'

interface ProjectSecretsSectionProps {
  projectName: string
  projectReady: boolean
}

/**
 * Lets a project owner set values a preview can read as environment
 * variables, without those values ever round-tripping through the browser.
 */
function ProjectSecretsSection({
  projectName,
  projectReady,
}: ProjectSecretsSectionProps) {
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      projectReady ? fetchProjectSecrets(projectName, signal) : Promise.resolve(null),
    [projectName, projectReady],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [
    projectName,
    projectReady,
  ])

  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [saveBusy, setSaveBusy] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [importBusy, setImportBusy] = useState(false)
  const [importError, setImportError] = useState<string | null>(null)
  const [importNotice, setImportNotice] = useState<string | null>(null)

  const [pending, setPending] = useState<ProjectSecretName | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  if (!projectReady) return null

  const nameValid = SECRET_NAME_PATTERN.test(name)
  const valueBytes = new TextEncoder().encode(value).length
  const valueValid = valueBytes <= SECRET_VALUE_MAX_BYTES
  const canSave = nameValid && valueValid && value !== '' && !saveBusy

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!nameValid) {
      setSaveError(
        'Name must start with a letter or underscore, then letters, digits, or underscores.',
      )
      return
    }
    if (!valueValid) {
      setSaveError(`Value must be at most ${SECRET_VALUE_MAX_BYTES} bytes.`)
      return
    }
    setSaveBusy(true)
    setSaveError(null)
    try {
      await setProjectSecrets(projectName, { [name]: value })
      setValue('')
      reload()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setSaveBusy(false)
    }
  }

  const runImport = async () => {
    setImportBusy(true)
    setImportError(null)
    setImportNotice(null)
    try {
      const result = await importProjectSecrets(projectName)
      setImportNotice(
        result.imported.length === 0
          ? `No new variables found in .env. Skipped: ${result.skipped.join(', ') || 'none'}.`
          : `Imported ${result.imported.join(', ')}.${
              result.skipped.length > 0 ? ` Skipped: ${result.skipped.join(', ')}.` : ''
            }`,
      )
      reload()
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setImportBusy(false)
    }
  }

  const confirmDelete = async (secret: ProjectSecretName) => {
    setDeleteBusy(true)
    setDeleteError(null)
    try {
      await deleteProjectSecret(projectName, secret.name)
      setPending(null)
      reload()
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setDeleteBusy(false)
    }
  }

  const names = data?.names ?? []

  return (
    <section className="card">
      <div className="card-header">
        <h2>Preview secrets</h2>
        <div className="button-row">
          <button
            type="button"
            className="small"
            onClick={() => void runImport()}
            disabled={importBusy}
          >
            {importBusy ? 'Importing…' : 'Import from .env'}
          </button>
          <button type="button" className="small" onClick={reload} disabled={loading}>
            {loading ? 'Working…' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="card-body">
        <p className="status">
          Values are held by the controller and never written into the
          sandbox until a preview starts. The API never returns a stored
          value once saved.
        </p>

        {error && (
          <p className="status status-error" role="alert">
            Failed to load secrets: {error}
          </p>
        )}

        {importError && (
          <p className="status status-error" role="alert">
            {importError}
          </p>
        )}
        {importNotice && <p className="status status-ok">{importNotice}</p>}

        {!error && !loading && names.length === 0 && (
          <p className="status">
            No secrets are stored for this project yet. Use Import from .env
            or the form below to add one.
          </p>
        )}

        {!error && names.length > 0 && (
          <dl className="detail-grid">
            {names.map((secret) => (
              <Fragment key={secret.name}>
                <dt className="mono">{secret.name}</dt>
                <dd>
                  <span title={formatTimestamp(secret.updated_at)}>
                    {formatRelativeTime(secret.updated_at)}
                  </span>{' '}
                  <button
                    type="button"
                    className="small danger"
                    onClick={() => {
                      setPending(secret)
                      setDeleteError(null)
                    }}
                  >
                    Remove
                  </button>
                </dd>
              </Fragment>
            ))}
          </dl>
        )}

        <form className="file-form" onSubmit={(event) => void save(event)}>
          <label>
            Name
            <input
              type="text"
              value={name}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Value
            <input
              type="password"
              value={value}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setValue(event.target.value)}
            />
          </label>
          <button type="submit" className="primary" disabled={!canSave}>
            {saveBusy ? 'Saving…' : 'Save'}
          </button>
        </form>

        {name !== '' && !nameValid && (
          <p className="status status-error">
            Name must start with a letter or underscore, then letters,
            digits, or underscores.
          </p>
        )}
        {!valueValid && (
          <p className="status status-error">
            Value is {valueBytes} bytes; the limit is {SECRET_VALUE_MAX_BYTES}.
          </p>
        )}
        {saveError && (
          <p className="status status-error" role="alert">
            {saveError}
          </p>
        )}
      </div>

      {pending && (
        <ConfirmDialog
          title="Remove this secret?"
          confirmPhrase={pending.name}
          confirmLabel="Remove secret"
          busy={deleteBusy}
          error={deleteError}
          onCancel={() => {
            setPending(null)
            setDeleteError(null)
          }}
          onConfirm={() => confirmDelete(pending)}
        >
          <p>
            This removes <span className="mono">{pending.name}</span>. A preview
            that reads it afterward starts without that variable.
          </p>
        </ConfirmDialog>
      )}
    </section>
  )
}

export default ProjectSecretsSection
