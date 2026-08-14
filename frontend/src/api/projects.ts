import { deleteJson, getJson, postJson } from './client'

export interface RemoteProject {
  project_id: string
  remote_url: string
  default_branch: string | null
  mirror_volume: string | null
  /** Null until the first sandbox is created, which fetches the mirror. */
  mirror_fetched_at: string | null
  sandbox_count: number
  created_at: string
}

export interface RemoteProjectsResponse {
  count: number
  projects: RemoteProject[]
}

export interface RemoveRemoteProjectResponse {
  project_id: string
  removed_mirror_volume: string | null
}

export function fetchRemoteProjects(
  signal?: AbortSignal,
): Promise<RemoteProjectsResponse> {
  return getJson<RemoteProjectsResponse>('/projects/remote', signal)
}

export function fetchRemoteProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<RemoteProject> {
  return getJson<RemoteProject>(
    `/projects/remote/${encodeURIComponent(projectId)}`,
    signal,
  )
}

export function registerRemoteProject(
  remoteUrl: string,
): Promise<RemoteProject> {
  return postJson<RemoteProject>('/projects/remote', { remote_url: remoteUrl })
}

export function removeRemoteProject(
  projectId: string,
): Promise<RemoveRemoteProjectResponse> {
  return deleteJson<RemoveRemoteProjectResponse>(
    `/projects/remote/${encodeURIComponent(projectId)}`,
  )
}
