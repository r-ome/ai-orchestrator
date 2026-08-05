import { deleteJson, getJson, postJson } from './client'

export interface RunningVolume {
  type: string
  name: string | null
  source: string
  destination: string
  driver: string
  mode: string
  read_write: boolean
  container_id: string
  container_name: string
}

export interface RunningVolumesResponse {
  count: number
  volumes: RunningVolume[]
}

export interface StorageUsage {
  total_count: number
  active_count: number
  size_bytes: number
  size: string
  reclaimable_bytes: number
  reclaimable: string
}

export interface DockerStorageStatusResponse {
  total_size_bytes: number
  total_size: string
  total_reclaimable_bytes: number
  total_reclaimable: string
  images: StorageUsage
  containers: StorageUsage
  volumes: StorageUsage
  build_cache: StorageUsage
}

export function fetchVolumes(
  signal?: AbortSignal,
): Promise<RunningVolumesResponse> {
  return getJson<RunningVolumesResponse>('/volumes', signal)
}

export function fetchStorageStatus(
  signal?: AbortSignal,
): Promise<DockerStorageStatusResponse> {
  return getJson<DockerStorageStatusResponse>('/volumes/status', signal)
}

/* --- Docker-managed volumes ------------------------------------------- */

export interface VolumeAttachment {
  container_id: string
  container_name: string
  container_status: string
  destination: string
  read_write: boolean
}

export interface ManagedVolume {
  name: string
  driver: string
  mountpoint: string
  created_at: string
  scope: string
  labels: Record<string, string> | null
  options: Record<string, string> | null
  attachments: VolumeAttachment[]
}

export interface ManagedVolumesResponse {
  count: number
  volumes: ManagedVolume[]
}

export interface RemoveVolumeResponse {
  name: string
  removed: boolean
}

export interface PruneVolumesResponse {
  deleted: string[]
  reclaimed_bytes: number
  reclaimed: string
}

export interface StopAttachedContainerResponse {
  volume_name: string
  container_id: string
  container_name: string
  status: string
}

export interface VolumeFileDetails {
  path: string
  name: string
  size_bytes: number
  mode: string
  modified_at: string
  link_target: string
  encoding: string
  content: string
}

export interface VolumeFileResponse {
  volume_name: string
  container_id: string
  container_name: string
  container_path: string
  file: VolumeFileDetails
}

export function fetchManagedVolumes(
  signal?: AbortSignal,
): Promise<ManagedVolumesResponse> {
  return getJson<ManagedVolumesResponse>('/volumes/all', signal)
}

export function fetchManagedVolume(
  volumeName: string,
  signal?: AbortSignal,
): Promise<ManagedVolume> {
  return getJson<ManagedVolume>(
    `/volumes/${encodeURIComponent(volumeName)}`,
    signal,
  )
}

export function fetchVolumeFile(
  volumeName: string,
  path: string,
  options: { containerId?: string; maxBytes?: number } = {},
  signal?: AbortSignal,
): Promise<VolumeFileResponse> {
  const query = new URLSearchParams({ path })
  if (options.containerId) query.set('container_id', options.containerId)
  if (options.maxBytes) query.set('max_bytes', String(options.maxBytes))

  return getJson<VolumeFileResponse>(
    `/volumes/${encodeURIComponent(volumeName)}/files?${query}`,
    signal,
  )
}

/* --- Destructive actions ---------------------------------------------- */
/* The backend rejects each of these unless confirm=true is sent. */

export function removeVolume(
  volumeName: string,
  options: { force?: boolean } = {},
): Promise<RemoveVolumeResponse> {
  const query = new URLSearchParams({ confirm: 'true' })
  if (options.force) query.set('force', 'true')

  return deleteJson<RemoveVolumeResponse>(
    `/volumes/${encodeURIComponent(volumeName)}?${query}`,
  )
}

export function pruneVolumes(): Promise<PruneVolumesResponse> {
  return postJson<PruneVolumesResponse>('/volumes/prune', { confirm: true })
}

export function stopAttachedContainer(
  volumeName: string,
  containerId: string,
  timeoutSeconds = 10,
): Promise<StopAttachedContainerResponse> {
  return postJson<StopAttachedContainerResponse>(
    `/volumes/${encodeURIComponent(volumeName)}/containers/${encodeURIComponent(containerId)}/stop`,
    { confirm: true, timeout_seconds: timeoutSeconds },
  )
}
