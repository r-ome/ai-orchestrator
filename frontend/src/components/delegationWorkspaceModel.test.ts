import { describe, expect, it } from 'vitest'
import type {
  DelegationView,
  FeatureChangeRequest,
  ImplementationContext,
  IntegrationReview,
  WorkItemRun,
  WorkItemView,
} from '../api/delegation'
import {
  selectCompletedItems,
  selectDisabledTabs,
  selectPhaseTab,
  selectReviewState,
  selectTabs,
  selectTurnToWatch,
  selectWorkspaceRows,
  shouldPollWorkspace,
  type WorkspaceData,
} from './delegationWorkspaceModel'

function makeContext(
  overrides: Partial<ImplementationContext> = {},
): ImplementationContext {
  return {
    id: 'context-1',
    status: 'ready',
    manifest: null,
    commands: [],
    provider: 'claude',
    model: 'model-1',
    error: null,
    created_at: '2026-08-19T00:00:00Z',
    ...overrides,
  }
}

function makeRun(id = 'run-1'): WorkItemRun {
  return {
    id,
    attempt: 1,
    status: 'running',
    provider: 'claude',
    model: 'model-1',
    routing_source: null,
    task_id: null,
    task_status: null,
    result: null,
    failure_kind: null,
    error: null,
    verification: null,
    usage: {
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
      cache_creation_tokens: null,
      cost_usd: null,
    },
    duration_ms: null,
    exit_code: null,
    repair_count: 0,
  }
}

function makeItem(overrides: Partial<WorkItemView> = {}): WorkItemView {
  return {
    item: {
      id: 'item-1',
      key: 'ITEM-1',
      title: 'First work item',
      objective: 'Do the work',
      scope: 'frontend/',
      out_of_scope: 'backend/',
      dependencies: [],
      files: [],
      symbols: [],
      write_scope: [],
      acceptance_criteria: [],
      verification: [],
      complexity: 'low',
      architecture: [],
      risks: [],
    },
    state: 'ready',
    wave: 1,
    blocked_by: [],
    can_run_in_parallel_with: [],
    runs: [],
    routing: null,
    ...overrides,
  }
}

function makeReview(overrides: Partial<IntegrationReview> = {}): IntegrationReview {
  return {
    id: 'review-1',
    revision: 1,
    status: 'completed',
    provider: 'claude',
    model: 'model-1',
    base_branch: null,
    base_commit: null,
    head_commit: null,
    approved: false,
    summary: '',
    findings: [],
    error: null,
    settled_at: '2026-08-19T00:00:00Z',
    source_merged_at: null,
    ...overrides,
  }
}

function makeChange(overrides: Partial<FeatureChangeRequest> = {}): FeatureChangeRequest {
  return {
    id: 'change-1',
    delegation_id: 'delegation-1',
    revision: 1,
    status: 'completed',
    instructions: 'Fix it',
    provider: 'claude',
    model: 'model-1',
    task_id: null,
    verification: null,
    error: null,
    created_at: '2026-08-19T00:00:00Z',
    updated_at: '2026-08-19T00:00:00Z',
    settled_at: '2026-08-19T00:00:00Z',
    ...overrides,
  }
}

function makeDelegation(overrides: Partial<DelegationView> = {}): DelegationView {
  return {
    delegation: {
      id: 'delegation-1',
      revision: 1,
      status: 'ready',
      context_id: 'context-1',
      error: null,
      created_at: '2026-08-19T00:00:00Z',
      settled_at: null,
    },
    items: [],
    waves: [],
    ready: [],
    review: null,
    changes: [],
    review_superseded: false,
    feature_approved: false,
    ...overrides,
  }
}

function rows(data: WorkspaceData | null) {
  return selectWorkspaceRows(data)
}

describe('selectWorkspaceRows', () => {
  it('returns null rows without data', () => {
    expect(rows(null)).toEqual({
      sessionContext: null,
      context: null,
      delegation: null,
      generatingContext: null,
      runningItem: null,
      runningChange: null,
    })
  })

  it('projects ready context and the first running rows', () => {
    const context = makeContext()
    const runningItem = makeItem({ state: 'running', runs: [makeRun()] })
    const runningChange = makeChange({ status: 'running' })
    const delegation = makeDelegation({
      items: [runningItem, makeItem({ state: 'running' })],
      changes: [runningChange, makeChange({ id: 'change-2', status: 'running' })],
    })

    expect(rows({ context, delegation })).toMatchObject({
      sessionContext: context,
      context,
      delegation,
      generatingContext: null,
      runningItem,
      runningChange,
    })
  })

  it('projects generating context without treating it as ready', () => {
    const context = makeContext({ status: 'generating' })
    expect(rows({ context, delegation: null })).toMatchObject({
      sessionContext: context,
      context: null,
      generatingContext: context,
    })
  })
})

describe('selectReviewState', () => {
  it('returns an empty review state without a delegation', () => {
    expect(selectReviewState(null)).toEqual({
      latestChange: null,
      runningChange: null,
      reviewSuperseded: false,
      featureApproved: false,
    })
  })

  it('selects the last change and the running one, whatever their order', () => {
    const awaiting = makeChange({ id: 'change-awaiting', status: 'awaiting_review' })
    const running = makeChange({ id: 'change-running', status: 'running' })
    const failed = makeChange({ id: 'change-failed', status: 'failed' })
    const state = selectReviewState(
      makeDelegation({ changes: [awaiting, running, failed] }),
    )

    // `latestChange` is the last entry, not the first and not the running one.
    expect(state.latestChange).toBe(failed)
    expect(state.runningChange).toBe(running)
  })

  it('reports no running change when none is running', () => {
    expect(
      selectReviewState(
        makeDelegation({
          changes: [makeChange({ status: 'awaiting_review' }), makeChange({ status: 'completed' })],
        }),
      ).runningChange,
    ).toBeNull()
  })

  it('approves a completed review when its incorporated change is earlier', () => {
    expect(
      selectReviewState(
        makeDelegation({
          review: makeReview({ approved: true, settled_at: '2026-08-20T00:00:00Z' }),
          changes: [makeChange({ created_at: '2026-08-18T00:00:00Z' })],
          review_superseded: false,
          feature_approved: true,
        }),
      ).featureApproved,
    ).toBe(true)
  })

  it.each([
    ['generating'],
    ['failed'],
  ] as const)('does not approve a %s review that still carries approved', (status) => {
    expect(
      selectReviewState(
        makeDelegation({
          review: makeReview({ status, approved: true, settled_at: null }),
          changes: [],
          review_superseded: false,
          feature_approved: false,
        }),
      ).featureApproved,
    ).toBe(false)
  })
})

describe('shouldPollWorkspace', () => {
  it.each([
    ['a generating context', { context: makeContext({ status: 'generating' }), delegation: null }],
    [
      'a running item',
      { context: null, delegation: makeDelegation({ items: [makeItem({ state: 'running' })] }) },
    ],
    [
      'a running delegation',
      {
        context: null,
        delegation: makeDelegation({
          delegation: { ...makeDelegation().delegation, status: 'running' },
        }),
      },
    ],
    [
      'a generating review',
      { context: null, delegation: makeDelegation({ review: makeReview({ status: 'generating' }) }) },
    ],
    [
      'a running change',
      { context: null, delegation: makeDelegation({ changes: [makeChange({ status: 'running' })] }) },
    ],
  ] as const)('polls for %s', (_case, data) => {
    expect(shouldPollWorkspace(data)).toBe(true)
  })

  it('does not poll when every row is settled', () => {
    expect(shouldPollWorkspace({ context: makeContext(), delegation: makeDelegation() })).toBe(false)
  })
})

describe('selectTurnToWatch', () => {
  it.each([
    [
      'context',
      { context: makeContext({ id: 'context-running', status: 'generating' }), delegation: null },
      { kind: 'context', jobId: 'context-running', title: 'Implementation context' },
    ],
    [
      'review',
      { context: makeContext(), delegation: makeDelegation({ review: makeReview({ status: 'generating' }) }) },
      { kind: 'review', jobId: 'review-1', title: 'Feature review' },
    ],
    [
      'change',
      {
        context: makeContext(),
        delegation: makeDelegation({ changes: [makeChange({ id: 'change-2', revision: 2, status: 'running' })] }),
      },
      { kind: 'change', jobId: 'change-2', title: 'Requested changes · revision 2' },
    ],
    [
      'latest run',
      {
        context: makeContext(),
        delegation: makeDelegation({ items: [makeItem({ state: 'running', runs: [makeRun('run-1'), makeRun('run-2')] })] }),
      },
      { kind: 'run', jobId: 'run-2', title: 'First work item' },
    ],
  ] as const)('selects the %s turn', (_case, data, turn) => {
    expect(selectTurnToWatch(rows(data))).toEqual(turn)
  })

  it('keeps the priority context, review, change, then item run', () => {
    const data = {
      context: makeContext({ id: 'context-running', status: 'generating' }),
      delegation: makeDelegation({
        review: makeReview({ status: 'generating' }),
        changes: [makeChange({ status: 'running' })],
        items: [makeItem({ state: 'running', runs: [makeRun()] })],
      }),
    }

    expect(selectTurnToWatch(rows(data))).toEqual({
      kind: 'context',
      jobId: 'context-running',
      title: 'Implementation context',
    })
  })

  it('keeps review ahead of a change and item run', () => {
    expect(
      selectTurnToWatch(
        rows({
          context: makeContext(),
          delegation: makeDelegation({
            review: makeReview({ status: 'generating' }),
            changes: [makeChange({ status: 'running' })],
            items: [makeItem({ state: 'running', runs: [makeRun()] })],
          }),
        }),
      ),
    ).toMatchObject({ kind: 'review', jobId: 'review-1' })
  })

  it('keeps a change ahead of an item run', () => {
    expect(
      selectTurnToWatch(
        rows({
          context: makeContext(),
          delegation: makeDelegation({
            changes: [makeChange({ status: 'running' })],
            items: [makeItem({ state: 'running', runs: [makeRun()] })],
          }),
        }),
      ),
    ).toMatchObject({ kind: 'change', jobId: 'change-1' })
  })

  it('returns null for a running item without runs', () => {
    expect(
      selectTurnToWatch(
        rows({ context: makeContext(), delegation: makeDelegation({ items: [makeItem({ state: 'running' })] }) }),
      ),
    ).toBeNull()
  })
})

describe('tab selectors', () => {
  it('counts completed items only', () => {
    expect(
      selectCompletedItems(
        makeDelegation({
          items: [
            makeItem({ state: 'completed' }),
            makeItem({ state: 'completed' }),
            makeItem({ state: 'ready' }),
          ],
        }),
      ),
    ).toBe(2)
    expect(selectCompletedItems(null)).toBe(0)
  })

  it.each([
    ['the workspace is disabled', false, makeContext(), makeDelegation(), true, true],
    ['neither context nor delegation exists', true, null, null, true, true],
    ['a ready context exists', true, makeContext(), null, false, true],
    ['an unfinished delegation exists', true, null, makeDelegation(), false, true],
    [
      'the delegation is complete',
      true,
      makeContext(),
      makeDelegation({ delegation: { ...makeDelegation().delegation, status: 'completed' } }),
      false,
      false,
    ],
  ] as const)('sets disabled tabs when %s', (_case, enabled, context, delegation, items, review) => {
    expect(selectDisabledTabs(enabled, context, delegation)).toEqual({
      items,
      'feature-review': review,
    })
  })

  it.each([
    ['running work', makeDelegation({ items: [makeItem({ state: 'running' })] }), 'running'],
    [
      'completion progress',
      makeDelegation({ items: [makeItem({ state: 'completed' }), makeItem({ state: 'ready' })] }),
      '1/2',
    ],
  ] as const)('sets the item badge for %s', (_case, delegation, badge) => {
    const tabs = selectTabs(rows({ context: makeContext(), delegation }), {
      items: false,
      'feature-review': true,
    })
    expect(tabs[0]).toMatchObject({ badge, disabled: false })
  })

  it.each([
    ['a running change', makeDelegation({ changes: [makeChange({ status: 'running' })] }), 'updating'],
    ['a generating review', makeDelegation({ review: makeReview({ status: 'generating' }) }), 'running'],
    ['an approved review', makeDelegation({ review: makeReview({ approved: true }) }), 'approved'],
    [
      'one finding',
      makeDelegation({
        review: makeReview({ findings: [{ severity: 'medium', text: 'One', work_item_keys: [] }] }),
      }),
      '1 finding',
    ],
    [
      'two findings',
      makeDelegation({
        review: makeReview({
          findings: [
            { severity: 'medium', text: 'One', work_item_keys: [] },
            { severity: 'medium', text: 'Two', work_item_keys: [] },
          ],
        }),
      }),
      '2 findings',
    ],
  ] as const)('sets the feature-review badge for %s', (_case, delegation, badge) => {
    const tabs = selectTabs(rows({ context: makeContext(), delegation }), {
      items: false,
      'feature-review': false,
    })
    expect(tabs[1]).toMatchObject({ badge, disabled: false })
  })

  it('keeps badges absent without a delegation', () => {
    const tabs = selectTabs(rows({ context: makeContext(), delegation: null }), {
      items: false,
      'feature-review': true,
    })
    expect(tabs).toEqual([
      { id: 'items', label: 'Work items', badge: undefined, disabled: false },
      { id: 'feature-review', label: 'Feature review', badge: undefined, disabled: true },
    ])
  })

  it.each([
    ['items for an incomplete delegation', makeDelegation(), 'items'],
    [
      'feature review for a complete delegation',
      makeDelegation({ delegation: { ...makeDelegation().delegation, status: 'completed' } }),
      'feature-review',
    ],
  ] as const)('selects %s', (_case, delegation, phaseTab) => {
    expect(selectPhaseTab(delegation)).toBe(phaseTab)
  })
})
