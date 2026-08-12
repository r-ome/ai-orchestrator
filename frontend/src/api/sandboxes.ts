import { deleteJson, getJson, postJson } from './client'

export type SandboxLifecycleStatus =
  | 'creating'
  | 'awaiting_engine_confirmation'
  | 'ready'
  | 'database_failed'
  | 'degraded'
  | 'destroying'

export interface Sandbox {
  sandbox_id: string
  project_id: string
  lifecycle_version: string | null
  feature_key: string | null
  feature_title: string | null
  desired_state: string | null
  lifecycle_status: SandboxLifecycleStatus | null
  base_ref: string | null
  feature_branch: string | null
  created_base_commit: string | null
  current_base_commit: string | null
  pending_base_commit: string | null
  db_engine: string | null
  db_name: string | null
  db_data_volume: string | null
  schema_baseline_hash: string | null
  remote_url: string | null
}

export interface SandboxesResponse {
  count: number
  sandboxes: Sandbox[]
}

export interface CreateSandboxBody {
  remote_url?: string
  feature_key: string
  feature_title?: string
  agent_provider?: string
  stop_blocking_previews?: boolean
  engine_confirmation?: ConfirmSandboxEngineBody
}

export interface DestroySandboxResponse {
  sandbox_id: string
  destroyed_at: string
  reason: string
}

export interface OrphanResource {
  resource: string
  kind: string
  name: string
  reported_at: string
}

export interface OrphanResourcesResponse {
  count: number
  resources: OrphanResource[]
}

export interface RemoveOrphanResourceResponse {
  resource: string
  removed: boolean
}

export interface SandboxStaleness {
  behind_count: number | null
  base_ref: string
  current_base_commit: string
  mirror_fetched_at: string | null
  stale_answer: boolean
  fetch_failure_reason: string | null
}

export interface EngineDetection {
  sandbox_id: string
  signals: Array<Record<string, unknown>>
  proposed_engine: 'mysql' | 'postgres' | 'sqlite' | 'none' | null
  confirmed_engine: 'mysql' | 'postgres' | 'sqlite' | 'none' | null
  migrate_commands: string[]
  seed_commands: string[]
  commands_source: Record<string, string>
  detected_at_commit: string
  actor: string | null
  confirmed_at: string | null
}

export interface ConfirmSandboxEngineBody {
  engine: 'mysql' | 'postgres' | 'sqlite' | 'none'
  migrate_commands: string[]
  seed_commands: string[]
  commands_source: Record<string, string>
  actor: string
}

export interface SyncSandboxBody {
  stop_blocking_preview: boolean
}

export interface EngineSyncReport {
  confirmed_engine: string | null
  detected_engine: string | null
  mismatch: boolean
  detection_error: string | null
}

export interface SyncSandboxResult extends Sandbox {
  operation_id: string
  safety_ref: string
  strategy: string
  engine_report: EngineSyncReport
}

export interface PublishSandboxResult {
  sandbox_id: string
  operation_id: string
  remote_branch: string
  last_pushed_commit: string
  remote_branch_sha: string
  pushed: boolean
  pr_number: number | null
  pr_url: string | null
  pr_state: string | null
}

export interface SandboxPublication {
  sandbox_id: string
  remote_branch: string
  last_pushed_commit: string | null
  remote_branch_sha: string | null
  pr_number: number | null
  pr_url: string | null
  pr_state: string | null
  last_error: string | null
  updated_at: string
}

export interface ResetSandboxDatabaseBody {
  stop_blocking_preview: boolean
}

/** V1 sandbox API. These calls use the immutable sandbox ID. */
export function fetchSandboxes(signal?: AbortSignal): Promise<SandboxesResponse> {
  return getJson<SandboxesResponse>('/sandboxes', signal)
}

export function fetchSandbox(
  sandboxId: string,
  signal?: AbortSignal,
): Promise<Sandbox> {
  return getJson<Sandbox>(`/sandboxes/${encodeURIComponent(sandboxId)}`, signal)
}

export function createSandbox(body: CreateSandboxBody): Promise<Sandbox> {
  return postJson<Sandbox>('/sandboxes', body)
}

export function removeSandbox(sandboxId: string): Promise<DestroySandboxResponse> {
  return deleteJson<DestroySandboxResponse>(
    `/sandboxes/${encodeURIComponent(sandboxId)}`,
  )
}

export function fetchSandboxStaleness(
  sandboxId: string,
  signal?: AbortSignal,
): Promise<SandboxStaleness> {
  return getJson<SandboxStaleness>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/staleness`,
    signal,
  )
}

export function syncSandbox(
  sandboxId: string,
  body: SyncSandboxBody,
): Promise<SyncSandboxResult> {
  return postJson<SyncSandboxResult>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/sync`,
    body,
  )
}

export function publishSandbox(sandboxId: string): Promise<PublishSandboxResult> {
  return postJson<PublishSandboxResult>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/publish`,
  )
}

export function fetchSandboxPublication(
  sandboxId: string,
  signal?: AbortSignal,
): Promise<SandboxPublication> {
  return getJson<SandboxPublication>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/publication`,
    signal,
  )
}

export function fetchSandboxEngine(
  sandboxId: string,
  signal?: AbortSignal,
): Promise<EngineDetection> {
  return getJson<EngineDetection>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/engine`,
    signal,
  )
}

export function confirmSandboxEngine(
  sandboxId: string,
  body: ConfirmSandboxEngineBody,
): Promise<Sandbox> {
  return postJson<Sandbox>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/confirm-engine`,
    body,
  )
}

export function resetSandboxDatabase(
  sandboxId: string,
  body: ResetSandboxDatabaseBody,
): Promise<Sandbox> {
  return postJson<Sandbox>(
    `/sandboxes/${encodeURIComponent(sandboxId)}/reset-db`,
    body,
  )
}

export function resumeSandbox(sandboxId: string): Promise<Sandbox> {
  return postJson<Sandbox>(`/sandboxes/${encodeURIComponent(sandboxId)}/resume`)
}

export function fetchOrphanResources(
  signal?: AbortSignal,
): Promise<OrphanResourcesResponse> {
  return getJson<OrphanResourcesResponse>('/sandboxes/orphans', signal)
}

export function removeOrphanResource(
  resource: string,
): Promise<RemoveOrphanResourceResponse> {
  return postJson<RemoveOrphanResourceResponse>(
    `/sandboxes/orphans/${encodeURIComponent(resource)}/remove`,
  )
}
