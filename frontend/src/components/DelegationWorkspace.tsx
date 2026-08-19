import { useCallback, useEffect, useState } from 'react'
import type { AgentProvider } from '../api/agents'
import {
  fetchContext,
  fetchDelegation,
  fetchDelegations,
  generateContext,
  type DelegationView,
  type ImplementationContext,
  type TurnKind,
  type WorkItemView,
} from '../api/delegation'
import {
  fetchPreviewCreationLogs,
  inspectPreview,
  startPreview,
  type PreviewLogs,
  type PreviewRun,
} from '../api/previews'
import { FeatureReviewPanel } from './FeatureReviewPanel'
import type { TabDefinition } from './Tabs'
import TurnConsole from './TurnConsole'
import { WorkItemsPanel } from './WorkItemsPanel'
import {
  EMPTY_DATA,
  selectDisabledTabs,
  selectPhaseTab,
  selectTabs,
  selectTurnToWatch,
  selectWorkspaceRows,
  shouldPollWorkspace,
  type DelegationTabId,
  type WatchedTurn,
  type WorkspaceData,
} from './delegationWorkspaceModel'
import { useApiResource } from '../hooks/useApiResource'

/**
 * The delegation phases that get a tab on the planning session page.
 *
 * Context generation is not one of them. It is a gate rather than a phase to
 * sit in: it runs once before work items can exist. Generating lives in
 * `ContextModal`, opened from "Implement this plan"; the result it produces is
 * read from the collapsed "Implementation context" card on the items tab.
 */
export type { DelegationTabId } from './delegationWorkspaceModel'


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
  previewFeature: () => void
  preview: PreviewRun | null
  previewLogs: PreviewLogs | null
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
  ], {
    // Every long phase settles its row from a background thread, so the page
    // has to poll to notice. A reload is also what turns a finished turn back
    // into a readable result.
    pollWhile: shouldPollWorkspace,
    intervalMs: 2_000,
  })
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)
  // A sandbox runs one preview at a time. This preview represents the current
  // integrated feature, not an individual work-item branch.
  const [preview, setPreview] = useState<PreviewRun | null>(null)
  const [previewLogs, setPreviewLogs] = useState<PreviewLogs | null>(null)
  const [previewProposalId, setPreviewProposalId] = useState<string | null>(null)
  const [watching, setWatching] = useState<WatchedTurn | null>(null)
  const [contextProvider, setContextProvider] = useState<AgentProvider>('claude')
  const [contextModel, setContextModel] = useState('')
  const [contextModalOpen, setContextModalOpen] = useState(false)
  // Held here rather than in the modal so closing and reopening it mid-turn
  // does not lose track of the row the turn is writing.
  const [awaitingContextId, setAwaitingContextId] = useState<string | null>(null)

  // One row, so "the context" and "the one being generated" are the same row
  // read through its status rather than two separate lookups.
  const rows = selectWorkspaceRows(data)
  const {
    sessionContext,
    context,
    delegation,
    generatingContext,
    runningItem,
    runningChange,
  } = rows

  useEffect(() => {
    setActionError(null)
    setWatching(null)
    setPreview(null)
    setPreviewLogs(null)
    setPreviewProposalId(null)
    setContextProvider('claude')
    setContextModel('')
    setContextModalOpen(false)
    setAwaitingContextId(null)
  }, [projectName, sessionId])

  useEffect(() => {
    if (!previewProposalId) return
    let cancelled = false
    const refresh = async () => {
      try {
        const nextLogs = await fetchPreviewCreationLogs(projectName, previewProposalId)
        if (!cancelled) setPreviewLogs(nextLogs)
      } catch {
        // The start request reports the authoritative error through runAction.
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 750)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [previewProposalId, projectName])

  useEffect(() => {
    // Reattach after a page reload: the turn outlives the request that started
    // it, so an in-flight row is enough to know what to watch.
    if (watching) return
    const next = selectTurnToWatch(rows)
    if (next) {
      setWatching(next)
    }
    // `rows` is derived from the dependencies above. Keep these dependencies
    // unchanged so the reattach timing stays identical to the original hook.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watching, generatingContext, runningItem, runningChange, delegation])

  const runAction = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label)
    setActionError(null)
    try {
      await action()
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

  /** Builds a preview from the sandbox's current integrated feature. */
  const previewFeature = () => {
    setPreviewLogs(null)
    void runAction('preview-feature', async () => {
      const proposal = await inspectPreview(projectName)
      setPreviewProposalId(proposal.id)
      try {
        const run = await startPreview(
          projectName,
          proposal,
          proposal.config,
          'rebuild',
          false,
        )
        setPreview(run)
      } finally {
        try {
          setPreviewLogs(await fetchPreviewCreationLogs(projectName, proposal.id))
        } catch {
          // Keep the request error. Progress retrieval is supplementary.
        }
        setPreviewProposalId(null)
      }
    })
  }

  const disabledTabs = selectDisabledTabs(enabled, context, delegation)
  const tabs = selectTabs(rows, disabledTabs)
  const phaseTab = selectPhaseTab(delegation)

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
    watching,
    contextProvider,
    setContextProvider,
    contextModel,
    setContextModel,
    runAction,
    watchTurn,
    previewFeature,
    preview,
    previewLogs,
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
  const { loading, error, actionError } = workspace

  const [changeInstructions, setChangeInstructions] = useState('')

  return (
    <div className="agent-delegation-workspace">
      {loading && <p className="status">Loading implementation state…</p>}
      {error && <p className="status status-error">Failed to load delegation: {error}</p>}
      {actionError && (
        <p className="status status-error" role="alert">
          {actionError}
        </p>
      )}

      {tab === 'items' ? (
        <WorkItemsPanel workspace={workspace} />
      ) : (
        <FeatureReviewPanel
          workspace={workspace}
          changeInstructions={changeInstructions}
          setChangeInstructions={setChangeInstructions}
        />
      )}

    </div>
  )
}

/**
 * What the context turn reported about the repository.
 *
 * The confirmed commands sit above this in their own rows, because they are the
 * part the controller verified. Everything here is the model's own account, so
 * it is worth reading before a decomposition quotes it into every packet.
 */
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
