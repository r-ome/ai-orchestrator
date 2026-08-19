import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  DelegationView,
  ImplementationContext,
  WorkItemView,
} from '../api/delegation'
import type { PreviewProposal, PreviewRun } from '../api/previews'
import * as delegationApi from '../api/delegation'
import * as previewsApi from '../api/previews'
import { useDelegationWorkspace } from './DelegationWorkspace'

vi.mock('../api/delegation', async () => {
  const actual = await vi.importActual<typeof import('../api/delegation')>('../api/delegation')
  return {
    ...actual,
    fetchContext: vi.fn(),
    fetchDelegations: vi.fn(),
    fetchDelegation: vi.fn(),
  }
})

vi.mock('../api/previews', async () => {
  const actual = await vi.importActual<typeof import('../api/previews')>('../api/previews')
  return {
    ...actual,
    inspectPreview: vi.fn(),
    startPreview: vi.fn(),
    fetchPreviewCreationLogs: vi.fn(),
  }
})

const fetchContext = vi.mocked(delegationApi.fetchContext)
const fetchDelegations = vi.mocked(delegationApi.fetchDelegations)
const fetchDelegation = vi.mocked(delegationApi.fetchDelegation)
const inspectPreview = vi.mocked(previewsApi.inspectPreview)
const startPreview = vi.mocked(previewsApi.startPreview)
const fetchPreviewCreationLogs = vi.mocked(previewsApi.fetchPreviewCreationLogs)

interface WorkspaceFixture {
  context: ImplementationContext | null
  delegation: DelegationView | null
}

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

function makeWorkItem(
  overrides: Partial<WorkItemView> = {},
): WorkItemView {
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

function makeDelegation(
  overrides: Partial<DelegationView> = {},
): DelegationView {
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

function makePreviewProposal(): PreviewProposal {
  return {
    id: 'proposal-1',
    digest: 'digest-1',
    sandbox_id: 'sandbox-1',
    project_name: 'project-1',
    detected_mode: 'native',
    detected_runtime: 'vite',
    confidence: 'high',
    evidence: [],
    available_services: [],
    config: {
      mode: 'native',
      runtime: 'vite',
      image: 'node:22',
      install_command: 'npm install',
      start_command: 'npm run dev',
      container_port: 5173,
      host_port: null,
      selected_service: '',
      compose_file: '',
      dockerfile: '',
      network_access: 'isolated',
      expiry_minutes: 60,
      persistent_volumes: [],
      services: {},
      initialize: { commands: [] },
      environment: {},
    },
    protected_files: {},
    changes: [],
    approval_required: false,
    created_at: '2026-08-19T00:00:00Z',
    expires_at: '2026-08-19T01:00:00Z',
    required_environment: [],
    configured_environment: [],
    missing_environment: [],
  }
}

function makePreviewRun(): PreviewRun {
  return {
    id: 'preview-1',
    sandbox_id: 'sandbox-1',
    project_name: 'project-1',
    proposal_id: 'proposal-1',
    kind: 'live',
    task_id: null,
    commit_sha: null,
    mode: 'native',
    runtime: 'vite',
    status: 'running',
    selected_service: '',
    container_port: 5173,
    host_port: 5173,
    url: 'http://localhost:5173',
    network_access: 'isolated',
    created_at: '2026-08-19T00:00:00Z',
    started_at: '2026-08-19T00:00:00Z',
    expires_at: '2026-08-19T01:00:00Z',
    last_activity_at: '2026-08-19T00:00:00Z',
    containers: [],
    database_sharing: null,
  }
}

function setupWorkspace(initial: WorkspaceFixture, enabled = true) {
  let fixture = initial
  fetchContext.mockImplementation(async () => fixture.context)
  fetchDelegations.mockImplementation(async () => ({
    count: fixture.delegation ? 1 : 0,
    delegations: fixture.delegation ? [fixture.delegation.delegation] : [],
  }))
  fetchDelegation.mockImplementation(async () => fixture.delegation ?? makeDelegation())

  const hook = renderHook(
    ({ projectName, sessionId, enabled }) =>
      useDelegationWorkspace(projectName, sessionId, enabled),
    { initialProps: { projectName: 'project-1', sessionId: 'session-1', enabled } },
  )

  return {
    ...hook,
    update(next: WorkspaceFixture) {
      fixture = next
      act(() => hook.result.current.reload())
    },
    setFixture(next: WorkspaceFixture) {
      fixture = next
    },
  }
}

async function waitForLoaded(workspace: ReturnType<typeof setupWorkspace>) {
  await waitFor(() => expect(workspace.result.current.loading).toBe(false))
}

beforeEach(() => {
  vi.clearAllMocks()
  inspectPreview.mockResolvedValue(makePreviewProposal())
  startPreview.mockResolvedValue(makePreviewRun())
  fetchPreviewCreationLogs.mockResolvedValue({
    proposal_id: 'proposal-1',
    preview_id: 'preview-1',
    status: 'running',
    events: [],
    logs: {},
  })
})

describe('useDelegationWorkspace characterization', () => {
  it('returns empty data without fetching when disabled', async () => {
    const workspace = setupWorkspace(
      { context: makeContext(), delegation: makeDelegation() },
      false,
    )

    await waitForLoaded(workspace)

    expect(fetchContext).not.toHaveBeenCalled()
    expect(fetchDelegations).not.toHaveBeenCalled()
    expect(fetchDelegation).not.toHaveBeenCalled()
    expect(workspace.result.current).toMatchObject({
      context: null,
      sessionContext: null,
      delegation: null,
      loading: false,
      error: null,
    })
  })

  it('watches generating context first', async () => {
    const workspace = setupWorkspace({
      context: makeContext({ id: 'context-running', status: 'generating' }),
      delegation: null,
    })

    await waitFor(() =>
      expect(workspace.result.current.watching).toEqual({
        kind: 'context',
        jobId: 'context-running',
        title: 'Implementation context',
      }),
    )
  })

  it('watches a generating review after context is settled', async () => {
    const workspace = setupWorkspace({
      context: makeContext(),
      delegation: makeDelegation({
        review: {
          id: 'review-1',
          revision: 1,
          status: 'generating',
          provider: 'claude',
          model: 'model-1',
          base_branch: null,
          base_commit: null,
          head_commit: null,
          approved: null,
          summary: '',
          findings: [],
          error: null,
          settled_at: null,
          source_merged_at: null,
        },
      }),
    })

    await waitFor(() =>
      expect(workspace.result.current.watching).toEqual({
        kind: 'review',
        jobId: 'review-1',
        title: 'Feature review',
      }),
    )
  })

  it('watches a running change after review is settled', async () => {
    const workspace = setupWorkspace({
      context: makeContext(),
      delegation: makeDelegation({
        changes: [
          {
            id: 'change-2',
            delegation_id: 'delegation-1',
            revision: 2,
            status: 'running',
            instructions: 'Fix it',
            provider: 'claude',
            model: 'model-1',
            task_id: null,
            verification: null,
            error: null,
            created_at: '2026-08-19T00:00:00Z',
            updated_at: '2026-08-19T00:00:00Z',
            settled_at: null,
          },
        ],
      }),
    })

    await waitFor(() =>
      expect(workspace.result.current.watching).toEqual({
        kind: 'change',
        jobId: 'change-2',
        title: 'Requested changes · revision 2',
      }),
    )
  })

  it('watches the latest run on a running item', async () => {
    const workspace = setupWorkspace({
      context: makeContext(),
      delegation: makeDelegation({
        items: [
          makeWorkItem({
            state: 'running',
            runs: [
              {
                id: 'run-1',
                attempt: 1,
                status: 'failed',
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
              },
              {
                id: 'run-2',
                attempt: 2,
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
              },
            ],
          }),
        ],
      }),
    })

    await waitFor(() =>
      expect(workspace.result.current.watching).toEqual({
        kind: 'run',
        jobId: 'run-2',
        title: 'First work item',
      }),
    )
  })

  it('uses the watch priority when several rows are in flight', async () => {
    const workspace = setupWorkspace({
      context: makeContext({ id: 'context-running', status: 'generating' }),
      delegation: makeDelegation({
        review: {
          id: 'review-1',
          revision: 1,
          status: 'generating',
          provider: 'claude',
          model: 'model-1',
          base_branch: null,
          base_commit: null,
          head_commit: null,
          approved: null,
          summary: '',
          findings: [],
          error: null,
          settled_at: null,
          source_merged_at: null,
        },
        changes: [
          {
            id: 'change-1',
            delegation_id: 'delegation-1',
            revision: 1,
            status: 'running',
            instructions: 'Fix it',
            provider: 'claude',
            model: 'model-1',
            task_id: null,
            verification: null,
            error: null,
            created_at: '2026-08-19T00:00:00Z',
            updated_at: '2026-08-19T00:00:00Z',
            settled_at: null,
          },
        ],
        items: [makeWorkItem({ state: 'running' })],
      }),
    })

    await waitFor(() => expect(workspace.result.current.watching?.kind).toBe('context'))
  })

  it('leaves watching null for a running item with no runs', async () => {
    const workspace = setupWorkspace({
      context: makeContext(),
      delegation: makeDelegation({ items: [makeWorkItem({ state: 'running' })] }),
    })

    await waitForLoaded(workspace)
    expect(workspace.result.current.watching).toBeNull()
  })

  it('keeps an existing watch after later data changes', async () => {
    const workspace = setupWorkspace({
      context: makeContext({ id: 'context-running', status: 'generating' }),
      delegation: null,
    })

    await waitFor(() => expect(workspace.result.current.watching?.kind).toBe('context'))

    workspace.update({
      context: makeContext(),
      delegation: makeDelegation({
        changes: [
          {
            id: 'change-1',
            delegation_id: 'delegation-1',
            revision: 1,
            status: 'running',
            instructions: 'Fix it',
            provider: 'claude',
            model: 'model-1',
            task_id: null,
            verification: null,
            error: null,
            created_at: '2026-08-19T00:00:00Z',
            updated_at: '2026-08-19T00:00:00Z',
            settled_at: null,
          },
        ],
      }),
    })

    await waitForLoaded(workspace)
    expect(workspace.result.current.watching).toEqual({
      kind: 'context',
      jobId: 'context-running',
      title: 'Implementation context',
    })
  })

  it('clears a watch and then reattaches from the current data', async () => {
    const workspace = setupWorkspace({
      context: makeContext({ id: 'context-running', status: 'generating' }),
      delegation: null,
    })

    await waitFor(() => expect(workspace.result.current.watching?.kind).toBe('context'))
    act(() => workspace.result.current.clearWatch())

    await waitFor(() => expect(workspace.result.current.watching?.kind).toBe('context'))
  })

  it.each([
    ['running item', makeDelegation({ items: [makeWorkItem({ state: 'running' })] }), 'running'],
    [
      'completed count',
      makeDelegation({
        items: [
          makeWorkItem({ state: 'completed' }),
          makeWorkItem({ item: { ...makeWorkItem().item, id: 'item-2', key: 'ITEM-2' } }),
        ],
      }),
      '1/2',
    ],
  ] as const)('shows the work-items badge for %s', async (_case, delegation, badge) => {
    const workspace = setupWorkspace({ context: makeContext(), delegation })
    await waitForLoaded(workspace)
    expect(workspace.result.current.tabs[0].badge).toBe(badge)
  })

  it.each([
    [
      'approved review',
      makeDelegation({
        delegation: { ...makeDelegation().delegation, status: 'completed' },
        review: {
          id: 'review-1',
          revision: 1,
          status: 'completed',
          provider: 'claude',
          model: 'model-1',
          base_branch: null,
          base_commit: null,
          head_commit: null,
          approved: true,
          summary: '',
          findings: [],
          error: null,
          settled_at: '2026-08-19T00:00:00Z',
          source_merged_at: null,
        },
      }),
      'approved',
    ],
    [
      'one finding',
      makeDelegation({
        review: {
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
          findings: [{ severity: 'warn', text: 'One issue', work_item_keys: [] }],
          error: null,
          settled_at: '2026-08-19T00:00:00Z',
          source_merged_at: null,
        },
      }),
      '1 finding',
    ],
    [
      'two findings',
      makeDelegation({
        review: {
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
          findings: [
            { severity: 'warn', text: 'One issue', work_item_keys: [] },
            { severity: 'warn', text: 'Another issue', work_item_keys: [] },
          ],
          error: null,
          settled_at: '2026-08-19T00:00:00Z',
          source_merged_at: null,
        },
      }),
      '2 findings',
    ],
    [
      'updating change',
      makeDelegation({
        changes: [
          {
            id: 'change-1',
            delegation_id: 'delegation-1',
            revision: 1,
            status: 'running',
            instructions: 'Fix it',
            provider: 'claude',
            model: 'model-1',
            task_id: null,
            verification: null,
            error: null,
            created_at: '2026-08-19T00:00:00Z',
            updated_at: '2026-08-19T00:00:00Z',
            settled_at: null,
          },
        ],
      }),
      'updating',
    ],
  ] as const)('shows the feature-review badge for %s', async (_case, delegation, badge) => {
    const workspace = setupWorkspace({ context: makeContext(), delegation })
    await waitForLoaded(workspace)
    expect(workspace.result.current.tabs[1].badge).toBe(badge)
  })

  it.each([
    ['disabled before the plan settles', false, null, null, true, true],
    ['disabled without context or delegation', true, null, null, true, true],
    ['items enabled by ready context', true, makeContext(), null, false, true],
    ['items enabled by existing delegation', true, null, makeDelegation(), false, true],
    [
      'review enabled by completed delegation',
      true,
      makeContext(),
      makeDelegation({ delegation: { ...makeDelegation().delegation, status: 'completed' } }),
      false,
      false,
    ],
  ] as const)(
    'sets tab gating when %s',
    async (_case, enabled, context, delegation, itemsDisabled, reviewDisabled) => {
      const workspace = setupWorkspace({ context, delegation })
      workspace.rerender({ projectName: 'project-1', sessionId: 'session-1', enabled })
      await waitForLoaded(workspace)
      expect(workspace.result.current.disabledTabs).toEqual({
        items: itemsDisabled,
        'feature-review': reviewDisabled,
      })
    },
  )

  it.each([
    ['items before completion', makeDelegation(), 'items'],
    [
      'feature review after completion',
      makeDelegation({ delegation: { ...makeDelegation().delegation, status: 'completed' } }),
      'feature-review',
    ],
  ] as const)('selects the phase tab for %s', async (_case, delegation, phaseTab) => {
    const workspace = setupWorkspace({ context: makeContext(), delegation })
    await waitForLoaded(workspace)
    expect(workspace.result.current.phaseTab).toBe(phaseTab)
  })

  it.each([
    ['project name', 'project-2', 'session-1'],
    ['session ID', 'project-1', 'session-2'],
  ])('resets watching, preview, and modal state when the %s changes', async (_case, projectName, sessionId) => {
    const workspace = setupWorkspace({
      context: makeContext(),
      delegation: null,
    })

    await waitForLoaded(workspace)
    await act(async () => {
      await workspace.result.current.watchTurn(
        'watch-test',
        'delegation',
        'Delegation',
        async () => ({ job_id: 'delegation-1' }),
      )
    })
    expect(workspace.result.current.watching?.kind).toBe('delegation')
    act(() => workspace.result.current.openContextModal())
    act(() => workspace.result.current.previewFeature())
    await waitFor(() => expect(workspace.result.current.preview).not.toBeNull())
    expect(workspace.result.current.contextModalOpen).toBe(true)

    workspace.setFixture({ context: makeContext(), delegation: null })
    workspace.rerender({ projectName, sessionId, enabled: true })

    await waitFor(() => {
      expect(workspace.result.current.watching).toBeNull()
      expect(workspace.result.current.preview).toBeNull()
      expect(workspace.result.current.contextModalOpen).toBe(false)
    })
  })
})
