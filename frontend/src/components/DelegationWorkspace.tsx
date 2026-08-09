import { useCallback, useEffect, useState } from 'react'
import type { AgentProvider } from '../api/agents'
import {
  acceptRun,
  clearItemRouting,
  fetchContext,
  fetchDelegation,
  fetchDelegations,
  generateContext,
  generateDelegation,
  rejectRun,
  resumeDelegation,
  runIntegrationReview,
  setItemRouting,
  startWorkItem,
  type ContextManifest,
  type DelegationView,
  type ImplementationContext,
  type ItemRouting,
  type TurnKind,
  type WorkItemState,
  type WorkItemView,
} from '../api/delegation'
import { inspectPreview, startPreview, type PreviewRun } from '../api/previews'
import CollapsibleCard from './CollapsibleCard'
import ConfirmDialog from './ConfirmDialog'
import type { TabDefinition } from './Tabs'
import TurnConsole from './TurnConsole'
import { useApiResource } from '../hooks/useApiResource'

/**
 * The delegation phases that get a tab on the planning session page.
 *
 * Context generation is not one of them. It is a gate rather than a phase to
 * sit in: it runs once before work items can exist. Generating lives in
 * `ContextModal`, opened from "Implement this plan"; the result it produces is
 * read from the collapsed "Implementation context" card on the items tab.
 */
export type DelegationTabId = 'items' | 'feature-review'

/** The turn the workspace is currently watching, if any. */
interface WatchedTurn {
  kind: TurnKind
  jobId: string
  title: string
}

interface WorkspaceData {
  context: ImplementationContext | null
  delegation: DelegationView | null
}

interface PendingDecision {
  action: 'accept' | 'reject'
  runId: string
  itemTitle: string
}

const EMPTY_DATA: WorkspaceData = {
  context: null,
  delegation: null,
}

function stateLabel(state: WorkItemState): string {
  return {
    blocked: 'Blocked',
    ready: 'Ready',
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
  }[state]
}

function stateTone(state: WorkItemState): string {
  if (state === 'completed') return 'ok'
  if (state === 'failed') return 'err'
  if (state === 'blocked') return 'muted'
  return 'warn'
}

function money(value: number | null): string {
  return value === null ? 'not reported' : `$${value.toFixed(4)}`
}

function RoutingControls({
  routing,
  disabled,
  onSave,
  onClear,
}: {
  routing: ItemRouting
  disabled: boolean
  onSave: (provider: AgentProvider | null, model: string | null) => void
  onClear: () => void
}) {
  const [provider, setProvider] = useState<AgentProvider | ''>(
    routing.override_provider ?? '',
  )
  const [model, setModel] = useState(routing.override_model ?? '')

  return (
    <div className="delegation-routing">
      <label>
        Provider override
        <select
          value={provider}
          onChange={(event) => setProvider(event.target.value as AgentProvider | '')}
          disabled={disabled}
        >
          <option value="">Automatic</option>
          <option value="claude">Claude</option>
          <option value="codex">Codex</option>
        </select>
      </label>
      <label>
        Model override
        <input
          value={model}
          maxLength={100}
          placeholder={routing.recommended_model}
          onChange={(event) => setModel(event.target.value)}
          disabled={disabled}
        />
      </label>
      <div className="button-row">
        <button
          type="button"
          className="small"
          disabled={disabled || (!provider && !model.trim())}
          onClick={() => onSave(provider || null, model.trim() || null)}
        >
          Save override
        </button>
        <button
          type="button"
          className="small ghost"
          disabled={disabled || (!routing.override_provider && !routing.override_model)}
          onClick={onClear}
        >
          Clear
        </button>
      </div>
    </div>
  )
}

/**
 * Why "Run item" is disabled, or null when it is not.
 *
 * A disabled button with no reason beside it is unreadable: the delegation
 * status that actually gates it lives at the top of the panel, and the
 * dependency that gates it lives in another card.
 */
function runBlockedReason(entry: WorkItemView, delegation: DelegationView): string | null {
  if (entry.state === 'running') return null
  if (entry.state === 'completed') return 'This work item is already complete.'
  if (entry.state === 'blocked') {
    return `Blocked by ${entry.blocked_by.join(', ') || 'an upstream work item'}.`
  }
  const status = delegation.delegation.status
  if (status === 'halted') {
    return 'The delegation is halted. Resume it above to run work items again.'
  }
  if (status === 'completed') return 'The delegation is already complete.'
  if (status === 'abandoned') return 'The delegation was abandoned.'
  return null
}

function ItemCard({
  entry,
  delegation,
  busy,
  watching,
  projectName,
  sessionId,
  onReload,
  onRun,
  onDecision,
  onPreview,
  preview,
  onSetRouting,
  onClearRouting,
}: {
  entry: WorkItemView
  delegation: DelegationView
  busy: boolean
  /** Run id the workspace is streaming, so only that card shows a console. */
  watching: string | null
  projectName: string
  sessionId: string
  onReload: () => void
  onRun: () => void
  onDecision: (decision: PendingDecision) => void
  onPreview: (taskId: string) => void
  /** The running preview, when it belongs to this card's task. */
  preview: PreviewRun | null
  onSetRouting: (provider: AgentProvider | null, model: string | null) => void
  onClearRouting: () => void
}) {
  const latest = entry.runs[entry.runs.length - 1]
  const canRun =
    ['ready', 'failed'].includes(entry.state) &&
    ['ready', 'running'].includes(delegation.delegation.status)
  const blockedReason = runBlockedReason(entry, delegation)
  const canSettle = latest?.status === 'running' && latest.task_status === 'review'
  const verificationPassed = latest?.verification?.passed === true
  const isWatched = watching !== null && latest?.id === watching

  return (
    <CollapsibleCard
      title={entry.item.title}
      defaultOpen={entry.state === 'ready' || entry.state === 'running'}
      aside={
        <>
          <span className={`pill ${stateTone(entry.state)}`}>
            {stateLabel(entry.state)}
          </span>
          <span className="pill muted">{entry.item.complexity}</span>
        </>
      }
    >
      <p>{entry.item.objective}</p>
      <ul className="kv-rows">
        <li>
          <span className="kv-key">Scope</span>
          <span className="kv-value">{entry.item.scope}</span>
        </li>
        <li>
          <span className="kv-key">Dependencies</span>
          <span className="kv-value">
            {entry.item.dependencies.join(', ') || 'None'}
          </span>
        </li>
        <li>
          <span className="kv-key">Blocked by</span>
          <span className="kv-value">{entry.blocked_by.join(', ') || 'Nobody'}</span>
        </li>
        <li>
          <span className="kv-key">Can run in parallel</span>
          <span className="kv-value">
            {entry.can_run_in_parallel_with.join(', ') || 'No other item'}
          </span>
        </li>
        <li>
          <span className="kv-key">Files</span>
          <span className="kv-value mono">{entry.item.files.join(', ') || 'Not named'}</span>
        </li>
      </ul>

      <div className="section-heading">Acceptance criteria</div>
      <ul>
        {entry.item.acceptance_criteria.map((criterion) => (
          <li key={criterion}>{criterion}</li>
        ))}
      </ul>

      {entry.routing && (
        <>
          <div className="section-heading">Routing</div>
          <p className="status">
            Recommended: <span className="mono">{entry.routing.recommended_model}</span>.
            Chosen: <span className="mono">{entry.routing.provider}/{entry.routing.model}</span>
            {' '}via {entry.routing.source.replaceAll('_', ' ')}.
          </p>
          {entry.routing.warning && (
            <p className="status status-warning">{entry.routing.warning}</p>
          )}
          <RoutingControls
            key={`${entry.routing.override_provider}-${entry.routing.override_model}`}
            routing={entry.routing}
            disabled={busy || entry.state === 'running' || entry.state === 'completed'}
            onSave={onSetRouting}
            onClear={onClearRouting}
          />
        </>
      )}

      {latest && (
        <div className="delegation-run-summary">
          <div className="section-heading">Latest attempt</div>
          <div className="detail-status-row">
            <span className={`pill ${latest.status === 'succeeded' ? 'ok' : latest.status === 'failed' ? 'err' : 'warn'}`}>
              {latest.status}
            </span>
            <span className="pill muted">attempt {latest.attempt}</span>
            <span className={`pill ${verificationPassed ? 'ok' : 'muted'}`}>
              Verification {verificationPassed ? 'passed' : 'not passed'}
            </span>
            {latest.repair_count > 0 && (
              <span className="pill warn">{latest.repair_count} repair</span>
            )}
          </div>
          <p className="status">
            {latest.provider}/{latest.model ?? 'model not reported'} · {money(latest.usage.cost_usd)}
            {latest.duration_ms === null ? '' : ` · ${latest.duration_ms} ms`}
          </p>
          {latest.error && <p className="status status-error">{latest.error}</p>}
        </div>
      )}

      {isWatched && (
        <TurnConsole
          projectName={projectName}
          sessionId={sessionId}
          kind="run"
          jobId={watching}
          title={`${entry.item.title} — ${latest?.provider ?? 'model'}/${latest?.model ?? ''}`}
          onFinished={onReload}
        />
      )}

      {!canRun && blockedReason && <p className="status">{blockedReason}</p>}

      {preview && (
        <p className="status">
          Preview of this task's commit is {preview.status} at{' '}
          <a href={preview.url} target="_blank" rel="noreferrer">
            {preview.url}
          </a>
          . Nothing is merged yet — check it, then merge.
        </p>
      )}

      <div className="button-row delegation-item-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || !canRun}
          title={canRun ? undefined : blockedReason ?? undefined}
          onClick={onRun}
        >
          {entry.state === 'running' ? 'Running…' : 'Run item'}
        </button>
        {canSettle && latest && (
          <>
            {latest.task_id && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onPreview(latest.task_id as string)}
              >
                {preview ? 'Rebuild preview' : 'Preview'}
              </button>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecision({ action: 'accept', runId: latest.id, itemTitle: entry.item.title })}
            >
              Merge
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={() => onDecision({ action: 'reject', runId: latest.id, itemTitle: entry.item.title })}
            >
              Reject
            </button>
          </>
        )}
      </div>
    </CollapsibleCard>
  )
}

export interface DelegationWorkspace {
  projectName: string
  sessionId: string
  loading: boolean
  error: string | null
  actionError: string | null
  /** The session's one context, once its turn has landed a manifest. */
  context: ImplementationContext | null
  /** The same row whatever its status, including generating and failed. */
  sessionContext: ImplementationContext | null
  delegation: DelegationView | null
  generatingContext: ImplementationContext | null
  runningItem: WorkItemView | null
  contextModalOpen: boolean
  openContextModal: () => void
  closeContextModal: () => void
  /** The context row a generation started from the modal is waiting on. */
  awaitingContextId: string | null
  setAwaitingContextId: (id: string | null) => void
  /** Tab definitions for the three phases, with their badges and gating. */
  tabs: TabDefinition<DelegationTabId>[]
  /** The phase the delegation has reached, used until the reader picks a tab. */
  phaseTab: DelegationTabId
  disabledTabs: Record<DelegationTabId, boolean>
  reload: () => void
  busy: string
  pending: PendingDecision | null
  setPending: (decision: PendingDecision | null) => void
  watching: WatchedTurn | null
  contextProvider: AgentProvider
  setContextProvider: (provider: AgentProvider) => void
  contextModel: string
  setContextModel: (model: string) => void
  runAction: (label: string, action: () => Promise<unknown>) => Promise<void>
  watchTurn: (
    label: string,
    kind: TurnKind,
    title: string,
    claim: () => Promise<{ job_id: string } | { id: string }>,
  ) => Promise<string | null>
  previewTask: (taskId: string) => void
  preview: PreviewRun | null
  setPreview: (preview: PreviewRun | null) => void
  clearWatch: () => void
}

/**
 * Loads and drives the delegation phases for one planning session.
 *
 * Split from the panels below so the planning session page can fold the three
 * phases into its own tab strip: the page owns which tab is showing, this hook
 * owns everything behind them.
 *
 * `enabled` is false until the plan settles. Delegation has nothing to say
 * before then, and asking for it would put two failing requests behind every
 * poll of a session that is still clarifying.
 */
export function useDelegationWorkspace(
  projectName: string,
  sessionId: string,
  enabled: boolean,
): DelegationWorkspace {
  const fetcher = useCallback(
    async (signal: AbortSignal): Promise<WorkspaceData> => {
      if (!enabled) return EMPTY_DATA
      const [context, delegations] = await Promise.all([
        fetchContext(projectName, sessionId, signal),
        fetchDelegations(projectName, sessionId, signal),
      ])
      const delegation = delegations.delegations[0]
        ? await fetchDelegation(projectName, sessionId, delegations.delegations[0].id, signal)
        : null
      return { context, delegation }
    },
    [projectName, sessionId, enabled],
  )
  const { data, loading, error, reload } = useApiResource(fetcher, [
    projectName,
    sessionId,
    enabled,
  ])
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingDecision | null>(null)
  // A sandbox runs one preview at a time, so this is the workspace's preview,
  // not the card's. The card it belongs to is found by matching the task id.
  const [preview, setPreview] = useState<PreviewRun | null>(null)
  const [watching, setWatching] = useState<WatchedTurn | null>(null)
  const [contextProvider, setContextProvider] = useState<AgentProvider>('claude')
  const [contextModel, setContextModel] = useState('')
  const [contextModalOpen, setContextModalOpen] = useState(false)
  // Held here rather than in the modal so closing and reopening it mid-turn
  // does not lose track of the row the turn is writing.
  const [awaitingContextId, setAwaitingContextId] = useState<string | null>(null)

  // One row, so "the context" and "the one being generated" are the same row
  // read through its status rather than two separate lookups.
  const sessionContext = data?.context ?? null
  const context = sessionContext?.status === 'ready' ? sessionContext : null
  const delegation = data?.delegation ?? null
  const generatingContext =
    sessionContext?.status === 'generating' ? sessionContext : null
  const runningItem =
    delegation?.items.find((entry) => entry.state === 'running') ?? null

  useEffect(() => {
    setActionError(null)
    setWatching(null)
    setContextProvider('claude')
    setContextModel('')
    setContextModalOpen(false)
    setAwaitingContextId(null)
  }, [projectName, sessionId])

  useEffect(() => {
    // Every long phase settles its row from a background thread, so the page
    // has to poll to notice. A reload is also what turns a finished turn back
    // into a readable result.
    const busyPhase =
      generatingContext !== null ||
      runningItem !== null ||
      delegation?.delegation.status === 'running' ||
      delegation?.review?.status === 'generating'
    if (!busyPhase) return
    const timer = window.setInterval(reload, 2_000)
    return () => window.clearInterval(timer)
  }, [generatingContext, runningItem, delegation, reload])

  useEffect(() => {
    // Reattach after a page reload: the turn outlives the request that started
    // it, so an in-flight row is enough to know what to watch.
    if (watching) return
    if (generatingContext) {
      setWatching({
        kind: 'context',
        jobId: generatingContext.id,
        title: 'Implementation context',
      })
      return
    }
    if (delegation?.review?.status === 'generating') {
      setWatching({
        kind: 'review',
        jobId: delegation.review.id,
        title: 'Feature review',
      })
      return
    }
    if (runningItem) {
      const latest = runningItem.runs[runningItem.runs.length - 1]
      if (latest) {
        setWatching({
          kind: 'run',
          jobId: latest.id,
          title: runningItem.item.title,
        })
      }
    }
  }, [watching, generatingContext, runningItem, delegation])

  const runAction = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label)
    setActionError(null)
    try {
      await action()
      setPending(null)
      reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setBusy('')
    }
  }

  /** Starts a turn and points the console at it.
   *
   *  The promise resolves when the claim is recorded, not when the turn ends —
   *  that is the whole point of the 202. Errors here are claim errors: a 409
   *  for a phase already running, or a 422 for a session that is not ready.
   */
  const watchTurn = async (
    label: string,
    kind: TurnKind,
    title: string,
    claim: () => Promise<{ job_id: string } | { id: string }>,
  ): Promise<string | null> => {
    let claimed: string | null = null
    await runAction(label, async () => {
      const accepted = await claim()
      const jobId = 'job_id' in accepted ? accepted.job_id : accepted.id
      claimed = jobId
      setWatching({ kind, jobId, title })
    })
    return claimed
  }

  /** Builds a preview from one task's own commit, before anything merges.
   *
   *  The commit is never sent: `_preview_target` reads it from the task row
   *  the controller wrote after checking the branch. A proposal is inspected
   *  first because the backend refuses a start whose digest it did not issue,
   *  and its detected config is used unedited — this is a confirmation step,
   *  not a place to reconfigure how the project builds.
   *
   *  Always 'rebuild', never 'start'. A sandbox holds one preview, and
   *  `start_preview` reads `action` only when one is already active, where
   *  'start' is a 409. This page cannot know about a preview begun on the
   *  project page or before a reload, so it replaces whatever is there.
   */
  const previewTask = (taskId: string) =>
    void runAction(`preview-${taskId}`, async () => {
      const proposal = await inspectPreview(projectName)
      const run = await startPreview(
        projectName,
        proposal,
        proposal.config,
        'rebuild',
        false,
        taskId,
      )
      setPreview(run)
    })

  const completedItems = delegation
    ? delegation.items.filter((entry) => entry.state === 'completed').length
    : 0

  const disabledTabs: Record<DelegationTabId, boolean> = {
    // Nothing to delegate until the plan settles, and nothing to decompose
    // until a context is ready. An existing delegation keeps its items
    // reachable even if no context row survives.
    items: !enabled || (context === null && delegation === null),
    'feature-review': !enabled || delegation?.delegation.status !== 'completed',
  }

  const tabs: TabDefinition<DelegationTabId>[] = [
    {
      id: 'items',
      label: 'Work items',
      badge: runningItem
        ? 'running'
        : delegation
          ? `${completedItems}/${delegation.items.length}`
          : undefined,
      disabled: disabledTabs.items,
    },
    {
      id: 'feature-review',
      label: 'Feature review',
      badge:
        delegation?.review?.status === 'generating'
          ? 'running'
          : delegation?.review?.status === 'completed'
            ? delegation.review.approved
              ? 'approved'
              : `${delegation.review.findings.length} finding${delegation.review.findings.length === 1 ? '' : 's'}`
            : undefined,
      disabled: disabledTabs['feature-review'],
    },
  ]

  const phaseTab: DelegationTabId =
    delegation?.delegation.status === 'completed' ? 'feature-review' : 'items'

  return {
    projectName,
    sessionId,
    loading: enabled && loading,
    error: enabled ? error : null,
    actionError,
    context,
    sessionContext,
    delegation,
    generatingContext,
    runningItem,
    contextModalOpen,
    openContextModal: () => setContextModalOpen(true),
    closeContextModal: () => setContextModalOpen(false),
    awaitingContextId,
    setAwaitingContextId,
    tabs,
    phaseTab,
    disabledTabs,
    reload,
    busy,
    pending,
    setPending,
    watching,
    contextProvider,
    setContextProvider,
    contextModel,
    setContextModel,
    runAction,
    watchTurn,
    previewTask,
    preview,
    setPreview,
    clearWatch: () => setWatching(null),
  }
}

/**
 * The panel body for one delegation phase.
 *
 * The caller renders the `role="tabpanel"` wrapper, so the panel ids stay in
 * one place next to the planning phases they share a tab strip with.
 */
export function DelegationPanel({
  tab,
  workspace,
}: {
  tab: DelegationTabId
  workspace: DelegationWorkspace
}) {
  const {
    projectName,
    sessionId,
    loading,
    error,
    actionError,
    context,
    delegation,
    generatingContext,
    reload,
    busy,
    pending,
    setPending,
    watching,
    openContextModal,
    runAction,
    watchTurn,
    previewTask,
    preview,
    setPreview,
    clearWatch,
  } = workspace

  const decide = () => {
    if (!pending || !delegation) return
    // The preview was built from the task's own commit. Once that commit is
    // merged or its branch deleted, the preview no longer shows anything the
    // buttons still act on, so the banner must not outlive the decision.
    setPreview(null)
    const action =
      pending.action === 'accept'
        ? acceptRun(projectName, sessionId, delegation.delegation.id, pending.runId)
        : rejectRun(
            projectName,
            sessionId,
            delegation.delegation.id,
            pending.runId,
            'Rejected by a person',
          )
    void runAction(pending.action, () => action)
  }

  const featureApproved =
    delegation?.review?.status === 'completed' &&
    delegation.review.approved === true

  return (
    <>
      {loading && <p className="status">Loading implementation state…</p>}
      {error && <p className="status status-error">Failed to load delegation: {error}</p>}
      {actionError && (
        <p className="status status-error" role="alert">
          {actionError}
        </p>
      )}

      {tab === 'items' && (
        <>
          <CollapsibleCard
            title="Implementation context"
            // Collapsed by default: once it is ready, the work items below are
            // what the reader came for. It opens to answer "which commands is
            // verification actually going to run?".
            defaultOpen={false}
            aside={
              generatingContext ? (
                <span className="pill warn">
                  generating
                </span>
              ) : context ? (
                <span className="pill ok">ready</span>
              ) : (
                <span className="pill muted">none</span>
              )
            }
          >
            <p>
              Repository pointers and controller-confirmed commands. Decomposition
              needs one before it can name files or verification.
            </p>
            {context ? (
              <>
                <ul className="kv-rows">
                  <li>
                    <span className="kv-key">Provider</span>
                    <span className="kv-value mono">{context.provider ?? 'not reported'}</span>
                  </li>
                  <li>
                    <span className="kv-key">Model</span>
                    <span className="kv-value mono">{context.model ?? 'not reported'}</span>
                  </li>
                  {context.commands.map((command) => (
                    <li key={command.kind}>
                      <span className="kv-key">
                        <span className={`pill ${command.confirmed ? 'ok' : 'warn'}`}>
                          {command.confirmed ? 'confirmed' : 'unconfirmed'}
                        </span>
                        {command.kind}
                      </span>
                      <span className="kv-value mono">{command.command}</span>
                    </li>
                  ))}
                </ul>
                <ContextManifestView manifest={context.manifest} />
              </>
            ) : (
              <p className="status">No implementation context has been generated yet.</p>
            )}
            <div className="button-row">
              <button
                type="button"
                disabled={Boolean(busy) || generatingContext !== null}
                onClick={openContextModal}
              >
                {generatingContext ? 'Generating context…' : 'Regenerate context'}
              </button>
            </div>
          </CollapsibleCard>

          {delegation && (
            <div className="detail-status-row">
              <span className="pill muted">revision {delegation.delegation.revision}</span>
              <span
                className={`pill ${delegation.delegation.status === 'completed' ? 'ok' : delegation.delegation.status === 'halted' ? 'err' : 'warn'}`}
              >
                {delegation.delegation.status}
              </span>
            </div>
          )}

          {!delegation && context && (
            <section className="card">
              <div className="card-header">
                <h2>Work-item decomposition</h2>
                <span className="pill ok">Context ready</span>
              </div>
              <div className="card-body">
                <p>
                  Confirmed commands:{' '}
                  {context.commands
                    .filter((command) => command.confirmed)
                    .map((command) => command.command)
                    .join(', ') || 'none'}
                </p>
                <button
                  type="button"
                  className="primary"
                  disabled={Boolean(busy) || watching?.kind === 'delegation'}
                  onClick={() =>
                    void watchTurn(
                      'delegation',
                      'delegation',
                      'Work-item decomposition',
                      () => generateDelegation(projectName, sessionId),
                    )
                  }
                >
                  {watching?.kind === 'delegation'
                    ? 'Generating work items…'
                    : 'Generate work items'}
                </button>

                {watching?.kind === 'delegation' && (
                  <TurnConsole
                    projectName={projectName}
                    sessionId={sessionId}
                    kind="delegation"
                    jobId={watching.jobId}
                    title={watching.title}
                    onFinished={() => {
                      clearWatch()
                      reload()
                    }}
                  />
                )}
              </div>
            </section>
          )}

          {delegation?.delegation.status === 'halted' ? (
            // A halt disables every Run item button, so the resume control sits
            // next to the buttons it re-enables.
            <section className="card card-alert" role="alert">
              <div className="card-header">
                <h2>Delegation halted</h2>
                <span className="pill err">halted</span>
              </div>
              <div className="card-body">
                <p>
                  Work items cannot run while the delegation is halted. It stopped
                  for this reason:
                </p>
                <p className="status status-error">
                  {delegation.delegation.error || 'No reason was recorded'}
                </p>
                <button
                  type="button"
                  className="primary"
                  disabled={Boolean(busy)}
                  onClick={() =>
                    void runAction('resume', () =>
                      resumeDelegation(projectName, sessionId, delegation.delegation.id),
                    )
                  }
                >
                  {busy === 'resume' ? 'Resuming…' : 'Resume and re-enable work items'}
                </button>
              </div>
            </section>
          ) : (
            delegation?.delegation.error && (
              <p className="status status-error" role="alert">
                {delegation.delegation.error}
              </p>
            )
          )}

          {delegation &&
            delegation.waves.map((wave, index) => (
              <section className="delegation-wave" key={`wave-${index + 1}`}>
                <div className="delegation-wave-heading">
                  <h2>Wave {index + 1}</h2>
                  <span className="pill muted">
                    {wave.length} item{wave.length === 1 ? '' : 's'}
                  </span>
                </div>
                {wave.map((key) => {
                  const entry = delegation.items.find((item) => item.item.key === key)
                  if (!entry) return null
                  return (
                    <ItemCard
                      key={key}
                      entry={entry}
                      delegation={delegation}
                      busy={Boolean(busy)}
                      watching={watching?.kind === 'run' ? watching.jobId : null}
                      projectName={projectName}
                      sessionId={sessionId}
                      onReload={reload}
                      onRun={() =>
                        void watchTurn(`run-${key}`, 'run', entry.item.title, () =>
                          startWorkItem(projectName, sessionId, delegation.delegation.id, key),
                        )
                      }
                      onDecision={setPending}
                      onPreview={previewTask}
                      preview={
                        preview &&
                        preview.task_id === entry.runs[entry.runs.length - 1]?.task_id
                          ? preview
                          : null
                      }
                      onSetRouting={(provider, model) =>
                        void runAction(`route-${key}`, () =>
                          setItemRouting(
                            projectName,
                            sessionId,
                            delegation.delegation.id,
                            key,
                            provider,
                            model,
                          ),
                        )
                      }
                      onClearRouting={() =>
                        void runAction(`route-${key}`, () =>
                          clearItemRouting(
                            projectName,
                            sessionId,
                            delegation.delegation.id,
                            key,
                          ),
                        )
                      }
                    />
                  )
                })}
              </section>
            ))}
        </>
      )}

      {tab === 'feature-review' && delegation && (
        <section className="card">
          <div className="card-header">
            <h2>Feature-level review</h2>
            {delegation.review?.status === 'completed' && (
              <span className={`pill ${delegation.review.approved ? 'ok' : 'warn'}`}>
                {delegation.review.approved ? 'Approved' : 'Findings remain'}
              </span>
            )}
          </div>
          <div className="card-body">
            {delegation.review ? (
              <>
                <p>{delegation.review.summary || delegation.review.error}</p>
                {delegation.review.findings.length > 0 && (
                  <ul className="kv-rows">
                    {delegation.review.findings.map((finding, index) => (
                      <li key={`${finding.text}-${index}`}>
                        <span className="kv-key">
                          <span className="pill warn">{finding.severity}</span>
                          {finding.work_item_keys.join(', ')}
                        </span>
                        <span className="kv-value">{finding.text}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p>
                Review the merged feature against the plan and controller-run
                verification.
              </p>
            )}
            <button
              type="button"
              className="primary feature-review-action"
              disabled={
                Boolean(busy) ||
                delegation.review?.status === 'generating' ||
                featureApproved
              }
              onClick={() =>
                void watchTurn('review', 'review', 'Feature review', () =>
                  runIntegrationReview(projectName, sessionId, delegation.delegation.id),
                )
              }
            >
              {delegation.review?.status === 'generating'
                ? 'Reviewing feature…'
                : featureApproved
                  ? 'Feature approved'
                  : delegation.review
                    ? 'Run review again'
                    : 'Run feature review'}
            </button>

            {watching?.kind === 'review' && (
              <TurnConsole
                projectName={projectName}
                sessionId={sessionId}
                kind="review"
                jobId={watching.jobId}
                title={watching.title}
                onFinished={reload}
              />
            )}
          </div>
        </section>
      )}

      {pending && (
        <ConfirmDialog
          title={`${pending.action === 'accept' ? 'Merge' : 'Reject'} ${pending.itemTitle}`}
          confirmPhrase={pending.action === 'accept' ? 'MERGE' : 'REJECT'}
          confirmLabel={pending.action === 'accept' ? 'Merge' : 'Reject task'}
          busy={Boolean(busy)}
          error={actionError}
          onConfirm={decide}
          onCancel={() => setPending(null)}
        >
          <p>
            {pending.action === 'accept'
              ? 'This fast-forwards the reviewed task commit into the sandbox branch.'
              : 'This deletes the task branch. The work item remains available for another attempt.'}
          </p>
        </ConfirmDialog>
      )}
    </>
  )
}

/**
 * What the context turn reported about the repository.
 *
 * The confirmed commands sit above this in their own rows, because they are the
 * part the controller verified. Everything here is the model's own account, so
 * it is worth reading before a decomposition quotes it into every packet.
 */
function ContextManifestView({ manifest }: { manifest: ContextManifest | null }) {
  if (manifest === null) {
    return <p className="status">This context recorded no manifest.</p>
  }

  const notes: [string, string[]][] = [
    ['Architecture', manifest.architecture],
    ['Patterns', manifest.patterns],
    ['Constraints', manifest.constraints],
    ['Assumptions', manifest.assumptions],
  ]

  return (
    <>
      <div className="section-heading">Modules</div>
      {manifest.modules.length > 0 ? (
        <ul className="kv-rows">
          {manifest.modules.map((module) => (
            <li key={module.path}>
              <span className="kv-key mono">{module.path}</span>
              <span className="kv-value">{module.purpose}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="status">No modules were reported.</p>
      )}

      <div className="section-heading">Symbols</div>
      {manifest.symbols.length > 0 ? (
        <ul className="kv-rows">
          {manifest.symbols.map((symbol) => (
            <li key={`${symbol.location}:${symbol.name}`}>
              <span className="kv-key mono">{symbol.name}</span>
              <span className="kv-value">
                <span className="mono">{symbol.location}</span> — {symbol.role}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="status">No symbols were reported.</p>
      )}

      {notes.map(([heading, lines]) =>
        lines.length > 0 ? (
          <div key={heading}>
            <div className="section-heading">{heading}</div>
            <ul>
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
    </>
  )
}

/**
 * The gate between an approved plan and work items: one context turn.
 *
 * A modal rather than a tab because there is exactly one decision here — which
 * model reads the repository — and the reader is on their way somewhere else.
 * It closes itself the moment the turn lands a ready context.
 */
export function ContextModal({
  workspace,
  onReady,
}: {
  workspace: DelegationWorkspace
  /** Called after the context turns ready, once the modal has closed. */
  onReady: () => void
}) {
  const {
    projectName,
    sessionId,
    context,
    sessionContext,
    generatingContext,
    busy,
    actionError,
    watching,
    contextProvider,
    setContextProvider,
    contextModel,
    setContextModel,
    watchTurn,
    reload,
    closeContextModal,
    awaitingContextId,
    setAwaitingContextId,
  } = workspace

  const running = generatingContext !== null || Boolean(busy)
  // Only a turn this modal started is worth reporting on. A failure from an
  // earlier visit is history the card on the Work items tab already carries.
  const awaited =
    sessionContext !== null && sessionContext.id === awaitingContextId
      ? sessionContext
      : null

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeContextModal()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [closeContextModal])

  useEffect(() => {
    if (awaited === null) return
    if (awaited.status === 'ready') {
      setAwaitingContextId(null)
      closeContextModal()
      onReady()
      return
    }
    // A failed row stops the wait but keeps the modal open: its error is the
    // one thing the reader needs before deciding whether to try another model.
    if (awaited.status === 'failed') setAwaitingContextId(null)
  }, [awaited, setAwaitingContextId, closeContextModal, onReady])

  const generate = () =>
    void watchTurn('context', 'context', 'Implementation context', () =>
      generateContext(
        projectName,
        sessionId,
        contextProvider,
        contextModel.trim() || null,
      ),
    ).then((jobId) => {
      if (jobId) setAwaitingContextId(jobId)
    })

  return (
    <div className="dialog-backdrop" role="presentation">
      <div
        className="dialog dialog-wide"
        role="dialog"
        aria-modal="true"
        aria-label="Generate the implementation context"
      >
        <h2>Implementation context</h2>
        <div className="dialog-body">
          <p>
            Before the plan can be split into work items, a model reads the
            repository and reports where things live and which commands build,
            test and lint it. The controller runs each command it names and keeps
            only the ones that work.
          </p>
          {context && (
            <p className="status">
              A context is already ready. Generating again replaces it, and is
              refused once this session has a delegation built from it.
            </p>
          )}

          <div className="delegation-routing">
            <label>
              Provider
              <select
                value={contextProvider}
                onChange={(event) =>
                  setContextProvider(event.target.value as AgentProvider)
                }
                disabled={running}
              >
                <option value="claude">Claude</option>
                <option value="codex">Codex</option>
              </select>
            </label>
            <label>
              Model
              <input
                value={contextModel}
                maxLength={100}
                placeholder="Use provider default"
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setContextModel(event.target.value)}
                disabled={running}
              />
            </label>
          </div>

          {awaited?.status === 'failed' && (
            <p className="status status-error" role="alert">
              The context turn failed: {awaited.error ?? 'no reason was recorded'}
            </p>
          )}
          {actionError && (
            <p className="status status-error" role="alert">
              {actionError}
            </p>
          )}

          {watching?.kind === 'context' && (
            <TurnConsole
              projectName={projectName}
              sessionId={sessionId}
              kind="context"
              jobId={watching.jobId}
              title={watching.title}
              onFinished={reload}
            />
          )}
        </div>

        <div className="dialog-actions">
          <button type="button" onClick={closeContextModal}>
            {running ? 'Run in the background' : 'Close'}
          </button>
          <button
            type="button"
            className="primary"
            disabled={running}
            onClick={generate}
          >
            {generatingContext
              ? 'Generating…'
              : awaited?.status === 'failed'
                ? 'Try again'
                : context
                  ? 'Replace the context'
                  : 'Generate context'}
          </button>
        </div>
      </div>
    </div>
  )
}
