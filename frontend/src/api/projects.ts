import { deleteJson, deleteJsonBody, getJson, postJson } from './client'

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
  remote_url: string
  feature_key: string
  feature_title?: string
}

export interface EngineDetection {
  sandbox_id: string
  proposed_engine: 'mysql' | 'postgres' | 'sqlite' | null
  confirmed_engine: 'mysql' | 'postgres' | 'sqlite' | null
  migrate_commands: string[]
  seed_commands: string[]
  commands_source: Record<string, string>
}

export interface ConfirmSandboxEngineBody {
  engine: 'mysql' | 'postgres' | 'sqlite'
  migrate_commands: string[]
  seed_commands: string[]
  commands_source: Record<string, string>
  actor: string
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

export function removeSandbox(sandboxId: string): Promise<{ sandbox_id: string }> {
  return deleteJson<{ sandbox_id: string }>(
    `/sandboxes/${encodeURIComponent(sandboxId)}`,
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

export interface ProjectRegistration {
  sandbox_id: string
  name: string
  source_path: string
  volume_name: string
  created_at: string
  copy_mode: string
  file_count: number
  copied_bytes: number
  copied_size: string
  driver: string
  mountpoint: string
  copy_job_id: string
  copy_status: 'queued' | 'copying' | 'completed' | 'failed' | 'unknown'
  ready: boolean
  excluded_directories: string[]
}

export interface ProjectRegistrationsResponse {
  count: number
  projects: ProjectRegistration[]
}

export interface BrowseEntry {
  name: string
  path: string
  has_children: boolean
}

export interface BrowseResponse {
  root: string
  path: string
  /** Null when already at the configured project root. */
  parent: string | null
  entries: BrowseEntry[]
}

export interface ProjectCopyJobStatus {
  job_id: string
  sandbox_id: string
  project_name: string
  source_path: string
  volume_name: string
  status: 'queued' | 'copying' | 'completed' | 'failed' | 'unknown'
  docker_status: string
  ready: boolean
  created_at: string
  started_at: string
  finished_at: string
  exit_code: number | null
  error: string
  log_tail: string
  status_url: string
  excluded_directories: string[]
}

export interface ProjectCopyJobsResponse {
  count: number
  jobs: ProjectCopyJobStatus[]
}

export interface RemoveProjectResponse {
  project_name: string
  removed_containers: number
  removed_networks: number
  removed_volumes: number
}

/** Lists subfolders of `path`, or of the project root when path is omitted. */
export function browseFolders(
  path?: string,
  signal?: AbortSignal,
): Promise<BrowseResponse> {
  const query = path ? `?${new URLSearchParams({ path })}` : ''
  return getJson<BrowseResponse>(`/projects/browse${query}`, signal)
}

export function fetchProjects(
  signal?: AbortSignal,
): Promise<ProjectRegistrationsResponse> {
  return getJson<ProjectRegistrationsResponse>('/projects', signal)
}

/** @deprecated Legacy name route. Use `fetchSandbox` with a sandbox ID. */
export function fetchProject(
  projectName: string,
  signal?: AbortSignal,
): Promise<ProjectRegistration> {
  return getJson<ProjectRegistration>(
    `/projects/${encodeURIComponent(projectName)}`,
    signal,
  )
}

export function fetchCopyJobs(
  signal?: AbortSignal,
): Promise<ProjectCopyJobsResponse> {
  return getJson<ProjectCopyJobsResponse>('/projects/copies', signal)
}

export function fetchCopyJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<ProjectCopyJobStatus> {
  return getJson<ProjectCopyJobStatus>(
    `/projects/copies/${encodeURIComponent(jobId)}`,
    signal,
  )
}

/** @deprecated Legacy local-folder route. Use `createSandbox` for v1. */
export function createProjectSandbox(path: string): Promise<ProjectCopyJobStatus> {
  return postJson<ProjectCopyJobStatus>('/projects', { path })
}

/** @deprecated Legacy name route. Use `removeSandbox` with a sandbox ID. */
export function removeProject(
  projectName: string,
): Promise<RemoveProjectResponse> {
  return deleteJsonBody<RemoveProjectResponse>(
    `/projects/${encodeURIComponent(projectName)}`,
    { confirm: true },
  )
}
