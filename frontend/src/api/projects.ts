import { getJson, postJson } from './client'

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

/** Copies the host folder into the next numbered project sandbox. */
export function createProjectSandbox(path: string): Promise<ProjectCopyJobStatus> {
  return postJson<ProjectCopyJobStatus>('/projects', { path })
}
