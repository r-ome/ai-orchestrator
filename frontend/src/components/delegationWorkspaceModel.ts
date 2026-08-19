import type {
  DelegationView,
  ImplementationContext,
  TurnKind,
  WorkItemView,
} from '../api/delegation'
import type { TabDefinition } from './Tabs'

export type DelegationTabId = 'items' | 'feature-review'

/** The turn the workspace is currently watching, if any. */
export interface WatchedTurn {
  kind: TurnKind
  jobId: string
  title: string
}

export interface WorkspaceData {
  context: ImplementationContext | null
  delegation: DelegationView | null
}

export const EMPTY_DATA: WorkspaceData = {
  context: null,
  delegation: null,
}

export interface WorkspaceRows {
  sessionContext: ImplementationContext | null
  context: ImplementationContext | null
  delegation: DelegationView | null
  generatingContext: ImplementationContext | null
  runningItem: WorkItemView | null
  runningChange: DelegationView['changes'][number] | null
}

export function selectWorkspaceRows(data: WorkspaceData | null): WorkspaceRows {
  const sessionContext = data?.context ?? null
  const delegation = data?.delegation ?? null

  return {
    sessionContext,
    context: sessionContext?.status === 'ready' ? sessionContext : null,
    delegation,
    generatingContext: sessionContext?.status === 'generating' ? sessionContext : null,
    runningItem: delegation?.items.find((entry) => entry.state === 'running') ?? null,
    runningChange: delegation?.changes.find((change) => change.status === 'running') ?? null,
  }
}

export function shouldPollWorkspace(data: WorkspaceData): boolean {
  return (
    data.context?.status === 'generating' ||
    data.delegation?.items.some((entry) => entry.state === 'running') === true ||
    data.delegation?.delegation.status === 'running' ||
    data.delegation?.review?.status === 'generating' ||
    data.delegation?.changes.some((change) => change.status === 'running') === true
  )
}

export function selectTurnToWatch(rows: WorkspaceRows): WatchedTurn | null {
  if (rows.generatingContext) {
    return {
      kind: 'context',
      jobId: rows.generatingContext.id,
      title: 'Implementation context',
    }
  }
  if (rows.delegation?.review?.status === 'generating') {
    return {
      kind: 'review',
      jobId: rows.delegation.review.id,
      title: 'Feature review',
    }
  }
  if (rows.runningChange) {
    return {
      kind: 'change',
      jobId: rows.runningChange.id,
      title: `Requested changes · revision ${rows.runningChange.revision}`,
    }
  }
  if (rows.runningItem) {
    const latest = rows.runningItem.runs[rows.runningItem.runs.length - 1]
    if (latest) {
      return {
        kind: 'run',
        jobId: latest.id,
        title: rows.runningItem.item.title,
      }
    }
  }
  return null
}

export function selectCompletedItems(delegation: DelegationView | null): number {
  return delegation
    ? delegation.items.filter((entry) => entry.state === 'completed').length
    : 0
}

export function selectDisabledTabs(
  enabled: boolean,
  context: ImplementationContext | null,
  delegation: DelegationView | null,
): Record<DelegationTabId, boolean> {
  return {
    // Nothing to delegate until the plan settles, and nothing to decompose
    // until a context is ready. An existing delegation keeps its items
    // reachable even if no context row survives.
    items: !enabled || (context === null && delegation === null),
    'feature-review': !enabled || delegation?.delegation.status !== 'completed',
  }
}

export function selectTabs(
  rows: WorkspaceRows,
  disabledTabs: Record<DelegationTabId, boolean>,
): TabDefinition<DelegationTabId>[] {
  const completedItems = selectCompletedItems(rows.delegation)

  return [
    {
      id: 'items',
      label: 'Work items',
      badge: rows.runningItem
        ? 'running'
        : rows.delegation
          ? `${completedItems}/${rows.delegation.items.length}`
          : undefined,
      disabled: disabledTabs.items,
    },
    {
      id: 'feature-review',
      label: 'Feature review',
      badge: rows.runningChange
        ? 'updating'
        : rows.delegation?.review?.status === 'generating'
        ? 'running'
        : rows.delegation?.review?.status === 'completed'
          ? rows.delegation.review.approved
            ? 'approved'
            : `${rows.delegation.review.findings.length} finding${rows.delegation.review.findings.length === 1 ? '' : 's'}`
          : undefined,
      disabled: disabledTabs['feature-review'],
    },
  ]
}

export function selectPhaseTab(delegation: DelegationView | null): DelegationTabId {
  return delegation?.delegation.status === 'completed' ? 'feature-review' : 'items'
}
