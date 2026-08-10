import type { AgentProvider } from './agents'
import { deleteJson, getJson, postJson, putJson } from './client'

export type ContextStatus = 'generating' | 'ready' | 'failed'
export type DelegationStatus = 'ready' | 'running' | 'completed' | 'halted' | 'abandoned'
export type WorkItemState = 'blocked' | 'ready' | 'running' | 'completed' | 'failed'
export type RunStatus = 'running' | 'succeeded' | 'failed' | 'abandoned'

export interface ResolvedCommand {
  kind: string
  command: string
  confirmed: boolean
  reason: string
}

export interface ContextModule {
  path: string
  purpose: string
}

export interface ContextSymbol {
  name: string
  location: string
  role: string
}

/** Repository pointers the context turn reported. No copied source code. */
export interface ContextManifest {
  modules: ContextModule[]
  symbols: ContextSymbol[]
  architecture: string[]
  patterns: string[]
  constraints: string[]
  assumptions: string[]
  commands: Record<string, string>
}

export interface ImplementationContext {
  id: string
  status: ContextStatus
  manifest: ContextManifest | null
  commands: ResolvedCommand[]
  provider: AgentProvider | null
  model: string | null
  error: string | null
  created_at: string
}

export interface RunUsage {
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_creation_tokens: number | null
  cost_usd: number | null
}

export interface WorkItemRun {
  id: string
  attempt: number
  status: RunStatus
  provider: AgentProvider | null
  model: string | null
  routing_source: string | null
  task_id: string | null
  task_status: string | null
  result: Record<string, unknown> | null
  failure_kind: string | null
  error: string | null
  verification: Record<string, unknown> | null
  usage: RunUsage
  duration_ms: number | null
  exit_code: number | null
  repair_count: number
}

export interface VerificationIntent {
  command_kind: string
  reason: string
}

export interface WorkItem {
  id: string
  key: string
  title: string
  objective: string
  scope: string
  out_of_scope: string
  dependencies: string[]
  files: string[]
  symbols: string[]
  write_scope: string[]
  acceptance_criteria: string[]
  verification: VerificationIntent[]
  complexity: 'low' | 'medium' | 'high'
  architecture: string[]
  risks: string[]
}

export interface ItemRouting {
  recommended_model: string
  model: string
  source: string
  provider: AgentProvider
  override_provider: AgentProvider | null
  override_model: string | null
  warning: string | null
}

export interface WorkItemView {
  item: WorkItem
  state: WorkItemState
  wave: number
  blocked_by: string[]
  can_run_in_parallel_with: string[]
  runs: WorkItemRun[]
  routing: ItemRouting | null
}

export interface IntegrationFinding {
  severity: string
  text: string
  work_item_keys: string[]
}

export interface IntegrationReview {
  id: string
  revision: number
  status: 'generating' | 'completed' | 'failed'
  provider: AgentProvider | null
  model: string | null
  base_branch: string | null
  base_commit: string | null
  head_commit: string | null
  approved: boolean | null
  summary: string
  findings: IntegrationFinding[]
  error: string | null
  settled_at: string | null
  source_merged_at: string | null
}

export interface FeatureDiffFile {
  path: string
  additions: number | null
  deletions: number | null
  binary: boolean
}

export interface FeatureDiff {
  review_id: string | null
  source_path: string
  base_branch: string
  base_commit: string
  head_commit: string
  files: FeatureDiffFile[]
  additions: number
  deletions: number
  patch: string
  truncated: boolean
}

export interface MergeFeatureOutcome {
  review: IntegrationReview
  source_path: string
  branch: string
  head_commit: string
  already_merged: boolean
}

export interface FeatureChangeRequest {
  id: string
  delegation_id: string
  revision: number
  status: 'running' | 'awaiting_review' | 'completed' | 'failed'
  instructions: string
  provider: AgentProvider
  model: string
  task_id: string | null
  verification: Record<string, unknown> | null
  error: string | null
  created_at: string
  updated_at: string
  settled_at: string | null
}

export interface Delegation {
  id: string
  revision: number
  status: DelegationStatus
  context_id: string | null
  error: string | null
  created_at: string
  settled_at: string | null
}

export interface DelegationView {
  delegation: Delegation
  items: WorkItemView[]
  waves: string[][]
  ready: string[]
  review: IntegrationReview | null
  changes: FeatureChangeRequest[]
}

export interface DelegationsResponse {
  count: number
  delegations: Delegation[]
}

export interface GenerateOutcome {
  accepted: boolean
  attempts: number
  validation_errors: string[]
  turn_status: string
  turn_error: string | null
}

export interface RunOutcome {
  delegation: DelegationView
  run_id: string
  run_status: RunStatus
  task_status: string | null
  result_errors: string[]
  routing_warning: string | null
}

export type TurnKind = 'context' | 'delegation' | 'run' | 'review' | 'change'

/** A turn the backend claimed and now runs in the background.
 *
 *  Decomposition, work item runs, and the feature review answer 202 with this
 *  instead of the finished result: a coding turn can take up to
 *  CODING_TURN_TIMEOUT_SECONDS (1800) and runs twice on a provider failure, so
 *  no fetch survives the wait. `job_id` is what `turnEventsUrl` streams. */
export interface AcceptedJob {
  job_id: string
  kind: TurnKind
  detail: string
}

export interface TurnProgress {
  type: 'progress'
  id: number
  created_at: string
  level: string
  step: string
  message: string
}

export interface TurnLogChunk {
  type: 'log'
  container: string
  data: string
}

export type TurnMessage = TurnProgress | TurnLogChunk | { type: 'end' }

function sessionPath(projectName: string, sessionId: string): string {
  return `/projects/${encodeURIComponent(projectName)}/planning/sessions/${encodeURIComponent(sessionId)}`
}

function delegationPath(projectName: string, sessionId: string): string {
  return `${sessionPath(projectName, sessionId)}/delegations`
}

/** The session's one context, or null before it has ever been generated. */
export function fetchContext(projectName: string, sessionId: string, signal?: AbortSignal) {
  return getJson<ImplementationContext | null>(
    `${sessionPath(projectName, sessionId)}/implementation-context`,
    signal,
  )
}

/** Opens the context for generation. Resolves once the row is `generating`, not
 *  when the turn finishes — follow it with `turnEventsUrl(..., 'context', id)`. */
export function generateContext(
  projectName: string,
  sessionId: string,
  provider: AgentProvider,
  model: string | null,
) {
  return postJson<ImplementationContext>(
    `${sessionPath(projectName, sessionId)}/implementation-context`,
    { provider, model },
  )
}

export function fetchDelegations(projectName: string, sessionId: string, signal?: AbortSignal) {
  return getJson<DelegationsResponse>(delegationPath(projectName, sessionId), signal)
}

export function fetchDelegation(
  projectName: string,
  sessionId: string,
  delegationId: string,
  signal?: AbortSignal,
) {
  return getJson<DelegationView>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}`,
    signal,
  )
}

export function generateDelegation(projectName: string, sessionId: string) {
  return postJson<AcceptedJob>(`${delegationPath(projectName, sessionId)}/generate`, {})
}

export function resumeDelegation(
  projectName: string,
  sessionId: string,
  delegationId: string,
) {
  return postJson<DelegationView>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}/start`,
  )
}

/** Claims a work item and starts its coding turn in the background.
 *
 *  Accept and reject stay synchronous: they are git operations measured in
 *  milliseconds, not turns. */
export function startWorkItem(
  projectName: string,
  sessionId: string,
  delegationId: string,
  key: string,
) {
  return postJson<AcceptedJob>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}` +
      `/items/${encodeURIComponent(key)}/run`,
    {},
  )
}

export function acceptRun(
  projectName: string,
  sessionId: string,
  delegationId: string,
  runId: string,
) {
  return postJson<RunOutcome>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}` +
      `/runs/${encodeURIComponent(runId)}/accept`,
  )
}

export function rejectRun(
  projectName: string,
  sessionId: string,
  delegationId: string,
  runId: string,
  reason: string,
) {
  return postJson<RunOutcome>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}` +
      `/runs/${encodeURIComponent(runId)}/reject`,
    { reason },
  )
}

export function setItemRouting(
  projectName: string,
  sessionId: string,
  delegationId: string,
  key: string,
  provider: AgentProvider | null,
  model: string | null,
) {
  return putJson<DelegationView>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}` +
      `/items/${encodeURIComponent(key)}/routing`,
    { provider, model, actor: 'human' },
  )
}

export function clearItemRouting(
  projectName: string,
  sessionId: string,
  delegationId: string,
  key: string,
) {
  return deleteJson<DelegationView>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}` +
      `/items/${encodeURIComponent(key)}/routing`,
  )
}

export function runIntegrationReview(
  projectName: string,
  sessionId: string,
  delegationId: string,
) {
  return postJson<AcceptedJob>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}/review`,
    {},
  )
}

export function requestFeatureChanges(
  projectName: string,
  sessionId: string,
  delegationId: string,
  instructions: string,
) {
  return postJson<AcceptedJob>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}/changes`,
    { instructions },
  )
}

export function fetchFeatureDiff(
  projectName: string,
  sessionId: string,
  delegationId: string,
  signal?: AbortSignal,
) {
  return getJson<FeatureDiff>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}/diff`,
    signal,
  )
}

export function mergeFeature(
  projectName: string,
  sessionId: string,
  delegationId: string,
  reviewId: string,
) {
  return postJson<MergeFeatureOutcome>(
    `${delegationPath(projectName, sessionId)}/${encodeURIComponent(delegationId)}/merge`,
    { review_id: reviewId, confirm: true },
  )
}

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** Absolute ws:// or wss:// URL carrying one turn's progress and the assigned
 *  model's container output, behind the same `/api` prefix the REST calls use. */
export function turnEventsUrl(
  projectName: string,
  sessionId: string,
  kind: TurnKind,
  jobId: string,
): string {
  const url = new URL(
    `${API_BASE}${sessionPath(projectName, sessionId)}` +
      `/turns/${kind}/${encodeURIComponent(jobId)}/events`,
    window.location.href,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}
