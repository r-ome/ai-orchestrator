import { useCallback, useMemo, useState } from 'react'
import { fetchFeatureDiff, type IntegrationReview } from '../api/delegation'
import { useApiResource } from '../hooks/useApiResource'

function patchLineClass(line: string): string {
  if (line.startsWith('@@')) return 'feature-diff-hunk'
  if (
    line.startsWith('diff --git') ||
    line.startsWith('index ') ||
    line.startsWith('--- ') ||
    line.startsWith('+++ ') ||
    line.startsWith('new file ') ||
    line.startsWith('deleted file ') ||
    line.startsWith('similarity index ') ||
    line.startsWith('rename from ') ||
    line.startsWith('rename to ')
  ) {
    return 'feature-diff-meta'
  }
  if (line.startsWith('+')) return 'feature-diff-added'
  if (line.startsWith('-')) return 'feature-diff-removed'
  return 'feature-diff-context'
}

/** One file's slice of a unified patch. */
interface PatchFile {
  path: string
  lines: string[]
}

/**
 * Splits a unified patch into one entry per file.
 *
 * The backend returns every file in a single patch string, so the reader had
 * to scroll one blob to find a file. Git starts each file with `diff --git`,
 * which is the only reliable boundary: a `+++ b/…` line can be `/dev/null`
 * for a deletion, and content lines can look like anything.
 */
function splitPatchByFile(patch: string): PatchFile[] {
  if (!patch) return []
  const files: PatchFile[] = []
  let current: PatchFile | null = null

  for (const line of patch.split('\n')) {
    if (line.startsWith('diff --git ')) {
      current = { path: gitHeaderPath(line), lines: [line] }
      files.push(current)
      continue
    }
    // Anything before the first header belongs to no file; keep it visible
    // rather than dropping it, so a malformed patch is still readable.
    if (current === null) {
      current = { path: '', lines: [] }
      files.push(current)
    }
    current.lines.push(line)
  }
  return files
}

/**
 * The path from a `diff --git a/x b/x` line.
 *
 * Takes the b-side, which is the path after the change, and falls back to the
 * a-side for a deletion. Paths containing spaces make the split ambiguous, so
 * the halves are matched against each other before trusting either.
 */
function gitHeaderPath(line: string): string {
  const rest = line.slice('diff --git '.length)
  const parts = rest.split(' ')
  const half = Math.floor(parts.length / 2)
  const left = parts.slice(0, half).join(' ')
  const right = parts.slice(half).join(' ')
  const strip = (value: string) => value.replace(/^[ab]\//, '')
  if (parts.length % 2 === 0 && strip(left) === strip(right)) return strip(left)
  return strip(right) || strip(left) || rest
}

/** A file's line tally, read from the patch when numstat did not name it. */
function tallyLines(lines: string[]): { additions: number; deletions: number } {
  let additions = 0
  let deletions = 0
  for (const line of lines) {
    if (line.startsWith('+') && !line.startsWith('+++')) additions += 1
    else if (line.startsWith('-') && !line.startsWith('---')) deletions += 1
  }
  return { additions, deletions }
}

/** One file in the diff, collapsed until the reader opens it. */
function PatchFileView({
  file,
  additions,
  deletions,
  binary,
  defaultOpen,
}: {
  file: PatchFile
  additions: number
  deletions: number
  binary: boolean
  defaultOpen: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="feature-diff-file">
      <button
        type="button"
        className="feature-diff-file-toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="feature-diff-file-caret" aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
        <span className="mono feature-diff-file-path">{file.path || 'Patch'}</span>
        <span className="mono feature-diff-file-tally">
          {binary ? 'binary' : `+${additions} / −${deletions}`}
        </span>
      </button>
      {open && (
        <pre
          className="feature-diff-patch"
          aria-label={`Diff for ${file.path || 'the patch'}`}
        >
          {file.lines.map((line, index) => (
            <span key={index} className={patchLineClass(line)}>
              {`${line}\n`}
            </span>
          ))}
        </pre>
      )}
    </div>
  )
}

export function FeatureCodeDiff({
  projectName,
  sessionId,
  delegationId,
  review,
  revisionKey,
}: {
  projectName: string
  sessionId: string
  delegationId: string
  review: IntegrationReview | null
  revisionKey: string
}) {
  const fetcher = useCallback(
    (signal: AbortSignal) =>
      fetchFeatureDiff(projectName, sessionId, delegationId, signal),
    [projectName, sessionId, delegationId],
  )
  const diff = useApiResource(fetcher, [
    projectName,
    sessionId,
    delegationId,
    review?.id,
    revisionKey,
  ])
  const patchFiles = useMemo(
    () => splitPatchByFile(diff.data?.patch ?? ''),
    [diff.data?.patch],
  )
  // numstat is the authority on totals; the patch is the authority on content.
  // A binary file appears in numstat with no hunks, so the two lists differ.
  const tallyByPath = useMemo(() => {
    const map = new Map<string, { additions: number; deletions: number; binary: boolean }>()
    for (const file of diff.data?.files ?? []) {
      map.set(file.path, {
        additions: file.additions ?? 0,
        deletions: file.deletions ?? 0,
        binary: file.binary,
      })
    }
    return map
  }, [diff.data?.files])
  // Open a single file by default. Opening all of them reproduces the wall of
  // text this replaced.
  const singleFile = patchFiles.length === 1

  return (
    <section className="feature-diff-section" aria-labelledby="feature-diff-heading">
      <div className="feature-diff-heading-row">
        <div>
          <div className="section-heading" id="feature-diff-heading">Implemented code</div>
          {diff.data && (
            <p className="status mono">
              {diff.data.base_branch} · {diff.data.base_commit.slice(0, 12)} →{' '}
              {diff.data.head_commit.slice(0, 12)}
            </p>
          )}
        </div>
        {diff.data && (
          <span className="pill muted">
            {diff.data.files.length} file{diff.data.files.length === 1 ? '' : 's'} ·{' '}
            +{diff.data.additions} / −{diff.data.deletions}
          </span>
        )}
      </div>

      {diff.loading && <p className="status">Loading code diff…</p>}
      {diff.error && (
        <p className="status status-error" role="alert">
          Failed to load code diff: {diff.error}
        </p>
      )}

      {diff.data && (
        <>
          {diff.data.files.length === 0 && (
            <p className="status">The accepted feature commits contain no file changes.</p>
          )}

          {/* One collapsible section per file, so a 158-line file no longer
              buries the two-line one below it. */}
          {patchFiles.length > 0 && (
            <div className="feature-diff-files">
              {patchFiles.map((file, index) => {
                const known = tallyByPath.get(file.path)
                const counted = known ?? tallyLines(file.lines)
                return (
                  <PatchFileView
                    key={`${file.path}-${index}`}
                    file={file}
                    additions={counted.additions}
                    deletions={counted.deletions}
                    binary={known?.binary ?? false}
                    defaultOpen={singleFile}
                  />
                )
              })}
            </div>
          )}

          {/* A file numstat counted but the patch never described: a binary
              file, or one cut off by the size limit. */}
          {diff.data.files
            .filter((file) => !patchFiles.some((entry) => entry.path === file.path))
            .map((file) => (
              <div className="feature-diff-file" key={`absent-${file.path}`}>
                <div className="feature-diff-file-toggle" aria-disabled="true">
                  <span className="feature-diff-file-caret" aria-hidden="true" />
                  <span className="mono feature-diff-file-path">{file.path}</span>
                  <span className="mono feature-diff-file-tally">
                    {file.binary
                      ? 'binary'
                      : `+${file.additions ?? 0} / −${file.deletions ?? 0}`}
                  </span>
                </div>
              </div>
            ))}

          {diff.data.truncated && (
            <p className="status status-warning">
              The displayed patch stops at 500,000 bytes. The file totals cover the full change.
            </p>
          )}

        </>
      )}

    </section>
  )
}
