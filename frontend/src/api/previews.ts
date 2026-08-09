import { deleteJson, deleteJsonBody, getJson, postJson, putJson } from './client'

export type PreviewMode = 'native' | 'dockerfile' | 'compose' | 'unknown'
export type PreviewRuntime =
  | 'static'
  | 'vite'
  | 'astro'
  | 'nextjs'
  | 'fastapi'
  | 'unknown'
export type PreviewNetworkAccess = 'isolated' | 'internet'
export type PreviewAction = 'start' | 'reuse' | 'restart' | 'rebuild'
export type PreviewPersistence = 'ephemeral' | 'persistent'

export type PreviewSharing = 'isolated' | 'shared_server' | 'shared_data'

export interface PreviewDependencyService {
  type: 'mysql'
  image: string
  database: string
  persistence: PreviewPersistence
  sharing: PreviewSharing
  /** Sandbox whose schema this sandbox joins. Only set for shared_data. */
  share_target: string
}

export interface SharedDatabaseCandidate {
  sandbox_id: string
  project_name: string
  schema_name: string
  image: string
  persistence: PreviewPersistence
  attached_sandboxes: number
  created_at: string
}

export interface DatabaseSharingState {
  sandbox_id: string
  sharing: PreviewSharing
  schema_name: string
  owner_sandbox_id: string
  owner_project_name: string
  image: string
  persistence: PreviewPersistence
  server_container: string
  attached_project_names: string[]
}

export interface ProjectDatabaseSharing {
  project_name: string
  sandbox_id: string
  current: DatabaseSharingState | null
  candidates: SharedDatabaseCandidate[]
}

export interface PreviewInitialization {
  commands: string[]
}

export interface PreviewEnvironmentSource {
  from_service: string
  from_secret: string
}

export interface PreviewConfiguration {
  mode: PreviewMode
  runtime: PreviewRuntime
  image: string
  install_command: string
  start_command: string
  container_port: number
  host_port: number | null
  selected_service: string
  compose_file: string
  dockerfile: string
  network_access: PreviewNetworkAccess
  expiry_minutes: number
  persistent_volumes: string[]
  services: Record<string, PreviewDependencyService>
  initialize: PreviewInitialization
  environment: Record<string, PreviewEnvironmentSource>
}

export interface ProtectedFileChange {
  path: string
  change: 'added' | 'modified' | 'removed'
  current_hash: string
  baseline_hash: string
  diff: string
}

export interface PreviewProposal {
  id: string
  digest: string
  sandbox_id: string
  project_name: string
  detected_mode: PreviewMode
  detected_runtime: PreviewRuntime
  confidence: string
  evidence: string[]
  available_services: string[]
  config: PreviewConfiguration
  protected_files: Record<string, string>
  changes: ProtectedFileChange[]
  approval_required: boolean
  created_at: string
  expires_at: string
  share_candidates: SharedDatabaseCandidate[]
  /** Variables the project appears to need. */
  required_environment: string[]
  /** Names the controller can supply: stored secrets plus managed DATABASE_URL. */
  configured_environment: string[]
  /** `required_environment` minus `configured_environment`. */
  missing_environment: string[]
}

export interface PreviewContainer {
  id: string
  name: string
  service: string
  status: string
}

export interface PreviewRun {
  id: string
  sandbox_id: string
  project_name: string
  proposal_id: string
  /** 'task' when built from one task's commit, 'live' from the working tree. */
  kind: 'live' | 'task'
  /** The task a 'task' preview was built from. Null for a live preview. */
  task_id: string | null
  commit_sha: string | null
  mode: PreviewMode
  runtime: PreviewRuntime
  status: string
  selected_service: string
  container_port: number
  host_port: number | null
  url: string
  network_access: PreviewNetworkAccess
  created_at: string
  started_at: string
  expires_at: string
  last_activity_at: string
  containers: PreviewContainer[]
  database_sharing: DatabaseSharingState | null
}

export interface PreviewLogs {
  proposal_id: string
  preview_id: string
  status: string
  events: PreviewProgressEvent[]
  logs: Record<string, string>
}

export interface PreviewProgressEvent {
  id: number
  level: string
  step: string
  message: string
  created_at: string
}

export interface StopPreviewResponse {
  id: string
  stopped: boolean
  removed_containers: number
  removed_networks: number
  removed_volumes: number
  removed_images: number
}

export function inspectPreview(projectName: string): Promise<PreviewProposal> {
  return postJson<PreviewProposal>(
    `/projects/${encodeURIComponent(projectName)}/preview-proposals`,
  )
}

/**
 * Naming a task is what makes this a task preview rather than a live one. The
 * backend reads that task's commit from its own row, so the preview shows the
 * task branch as the controller verified it — before any merge.
 */
export function startPreview(
  projectName: string,
  proposal: PreviewProposal,
  config: PreviewConfiguration,
  action: 'start' | 'rebuild',
  saveDefault: boolean,
  taskId = '',
): Promise<PreviewRun> {
  return postJson<PreviewRun>(
    `/projects/${encodeURIComponent(projectName)}/previews`,
    {
      proposal_id: proposal.id,
      proposal_digest: proposal.digest,
      config,
      action,
      actor: 'human',
      save_default: saveDefault,
      task_id: taskId,
    },
  )
}

export function fetchCurrentPreview(
  projectName: string,
  signal?: AbortSignal,
): Promise<PreviewRun> {
  return getJson<PreviewRun>(
    `/projects/${encodeURIComponent(projectName)}/previews/current`,
    signal,
  )
}

export function actOnPreview(
  projectName: string,
  action: 'reuse' | 'restart',
): Promise<PreviewRun> {
  return postJson<PreviewRun>(
    `/projects/${encodeURIComponent(projectName)}/previews/current/actions`,
    { action, confirm: true },
  )
}

export function keepPreviewAlive(
  projectName: string,
  expiryMinutes: number,
): Promise<PreviewRun> {
  return postJson<PreviewRun>(
    `/projects/${encodeURIComponent(projectName)}/previews/current/keep-alive`,
    { expiry_minutes: expiryMinutes },
  )
}

export function fetchPreviewLogs(projectName: string): Promise<PreviewLogs> {
  return getJson<PreviewLogs>(
    `/projects/${encodeURIComponent(projectName)}/previews/current/logs`,
  )
}

export function fetchPreviewCreationLogs(
  projectName: string,
  proposalId: string,
): Promise<PreviewLogs> {
  return getJson<PreviewLogs>(
    `/projects/${encodeURIComponent(projectName)}/preview-proposals/${encodeURIComponent(proposalId)}/logs`,
  )
}

export function stopPreview(
  projectName: string,
  removeDataVolumes: boolean,
): Promise<StopPreviewResponse> {
  return deleteJsonBody<StopPreviewResponse>(
    `/projects/${encodeURIComponent(projectName)}/previews/current`,
    { confirm: true, remove_data_volumes: removeDataVolumes },
  )
}

export function fetchDatabaseSharing(
  projectName: string,
  signal?: AbortSignal,
): Promise<ProjectDatabaseSharing> {
  return getJson<ProjectDatabaseSharing>(
    `/projects/${encodeURIComponent(projectName)}/database-sharing`,
    signal,
  )
}

export const SECRET_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/
export const SECRET_VALUE_MAX_BYTES = 8192
export const SECRET_MAX_COUNT = 100

export interface ProjectSecretName {
  name: string
  updated_at: string
}

export interface ProjectSecrets {
  project_name: string
  names: ProjectSecretName[]
}

export interface ImportProjectSecretsResponse {
  project_name: string
  imported: string[]
  skipped: string[]
}

export function fetchProjectSecrets(
  projectName: string,
  signal?: AbortSignal,
): Promise<ProjectSecrets> {
  return getJson<ProjectSecrets>(
    `/projects/${encodeURIComponent(projectName)}/secrets`,
    signal,
  )
}

export function setProjectSecrets(
  projectName: string,
  values: Record<string, string>,
): Promise<ProjectSecrets> {
  return putJson<ProjectSecrets>(
    `/projects/${encodeURIComponent(projectName)}/secrets`,
    { values },
  )
}

export function deleteProjectSecret(
  projectName: string,
  name: string,
): Promise<ProjectSecrets> {
  return deleteJson<ProjectSecrets>(
    `/projects/${encodeURIComponent(projectName)}/secrets/${encodeURIComponent(name)}`,
  )
}

export function importProjectSecrets(
  projectName: string,
): Promise<ImportProjectSecretsResponse> {
  return postJson<ImportProjectSecretsResponse>(
    `/projects/${encodeURIComponent(projectName)}/secrets/import`,
  )
}
