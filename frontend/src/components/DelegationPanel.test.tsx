import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type {
  DelegationView,
  FeatureChangeRequest,
  IntegrationReview,
} from '../api/delegation'
import {
  DelegationPanel,
  type DelegationWorkspace,
} from './DelegationWorkspace'

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
    approved: true,
    summary: '',
    findings: [],
    error: null,
    settled_at: '2026-08-20T00:00:00Z',
    source_merged_at: null,
    ...overrides,
  }
}

function makeChange(
  overrides: Partial<FeatureChangeRequest> = {},
): FeatureChangeRequest {
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
    created_at: '2026-08-18T00:00:00Z',
    updated_at: '2026-08-18T00:00:00Z',
    settled_at: '2026-08-18T00:00:00Z',
    ...overrides,
  }
}

function makeDelegation(overrides: Partial<DelegationView> = {}): DelegationView {
  return {
    delegation: {
      id: 'delegation-1',
      revision: 1,
      status: 'completed',
      context_id: 'context-1',
      error: null,
      created_at: '2026-08-18T00:00:00Z',
      settled_at: '2026-08-18T00:00:00Z',
    },
    items: [],
    waves: [],
    ready: [],
    review: makeReview(),
    changes: [],
    review_superseded: false,
    feature_approved: true,
    ...overrides,
  }
}

function makeWorkspace(
  overrides: Partial<DelegationWorkspace> = {},
): DelegationWorkspace {
  return {
    projectName: 'project-1',
    sessionId: 'session-1',
    loading: false,
    error: null,
    actionError: null,
    context: null,
    sessionContext: null,
    delegation: makeDelegation(),
    generatingContext: null,
    runningItem: null,
    contextModalOpen: false,
    openContextModal: vi.fn(),
    closeContextModal: vi.fn(),
    awaitingContextId: null,
    setAwaitingContextId: vi.fn(),
    tabs: [],
    phaseTab: 'feature-review',
    disabledTabs: { items: false, 'feature-review': false },
    reload: vi.fn(),
    busy: '',
    watching: null,
    contextProvider: 'claude',
    setContextProvider: vi.fn(),
    contextModel: '',
    setContextModel: vi.fn(),
    runAction: vi.fn(),
    watchTurn: vi.fn(),
    previewFeature: vi.fn(),
    preview: null,
    previewLogs: null,
    clearWatch: vi.fn(),
    ...overrides,
  }
}

function renderFeatureReview(delegation: DelegationView | null, busy = '') {
  render(
    <DelegationPanel
      tab="feature-review"
      workspace={makeWorkspace({ delegation, busy })}
    />,
  )
}

describe('DelegationPanel feature review', () => {
  it('shows an approved completed review without an incorporated change', () => {
    renderFeatureReview(makeDelegation())

    expect(screen.getByText('Approved')).toHaveClass('ok')
    expect(screen.getByRole('button', { name: 'Feature approved' })).toBeDisabled()
  })

  it('shows remaining findings for a completed review that is not approved', () => {
    renderFeatureReview(
      makeDelegation({
        review: makeReview({ approved: false }),
        feature_approved: false,
      }),
    )

    expect(screen.getByText('Findings remain')).toHaveClass('warn')
    expect(screen.getByRole('button', { name: 'Run review again' })).toBeEnabled()
  })

  it('requires review after a later incorporated change', () => {
    renderFeatureReview(
      makeDelegation({
        changes: [makeChange({ created_at: '2026-08-22T00:00:00Z' })],
        review_superseded: true,
        feature_approved: false,
      }),
    )

    expect(screen.getByText('Review needed')).toHaveClass('warn')
    expect(screen.getByRole('button', { name: 'Run review again' })).toBeEnabled()
  })

  it('keeps approval after an earlier incorporated change', () => {
    renderFeatureReview(
      makeDelegation({
        changes: [makeChange({ created_at: '2026-08-18T00:00:00Z' })],
        review_superseded: false,
        feature_approved: true,
      }),
    )

    expect(screen.getByText('Approved')).toHaveClass('ok')
    expect(screen.getByRole('button', { name: 'Feature approved' })).toBeDisabled()
  })

  it('uses the last incorporated change and ignores other change statuses', () => {
    renderFeatureReview(
      makeDelegation({
        changes: [
          makeChange({
            id: 'change-awaiting-review',
            status: 'awaiting_review',
            created_at: '2026-08-18T00:00:00Z',
          }),
          makeChange({
            id: 'change-running',
            status: 'running',
            created_at: '2026-08-23T00:00:00Z',
          }),
          makeChange({
            id: 'change-failed',
            status: 'failed',
            created_at: '2026-08-24T00:00:00Z',
          }),
          makeChange({
            id: 'change-completed',
            status: 'completed',
            created_at: '2026-08-22T00:00:00Z',
          }),
        ],
        review_superseded: true,
        feature_approved: false,
      }),
    )

    expect(screen.getByText('Review needed')).toHaveClass('warn')
    expect(screen.getByRole('button', { name: 'Run review again' })).toBeEnabled()
  })

  it('disables the button while the review generates', () => {
    renderFeatureReview(makeDelegation({ review: makeReview({ status: 'generating' }) }))

    expect(screen.getByRole('button', { name: 'Reviewing feature…' })).toBeDisabled()
  })

  it('offers the first review without rendering a pill', () => {
    renderFeatureReview(
      makeDelegation({
        review: null,
        review_superseded: false,
        feature_approved: false,
      }),
    )

    expect(screen.getByRole('button', { name: 'Run feature review' })).toBeEnabled()
    expect(screen.queryByText('Approved')).not.toBeInTheDocument()
    expect(screen.queryByText('Review needed')).not.toBeInTheDocument()
    expect(screen.queryByText('Findings remain')).not.toBeInTheDocument()
  })

  it('disables the review button while another action is busy', () => {
    renderFeatureReview(
      makeDelegation({
        review: makeReview({ approved: false }),
        feature_approved: false,
      }),
      'preview-feature',
    )

    expect(screen.getByRole('button', { name: 'Run review again' })).toBeDisabled()
  })

  it('does not render the feature-review section on the items tab', () => {
    render(
      <DelegationPanel
        tab="items"
        workspace={makeWorkspace()}
      />,
    )

    expect(screen.queryByRole('heading', { name: 'Feature-level review' })).not.toBeInTheDocument()
  })
})
