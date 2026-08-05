import { deleteJson, getJson, postJson } from './client'

export interface ContainerPort {
  container_port: number
  protocol: string
  host_ip: string | null
  host_port: number | null
}

export interface RunningContainer {
  id: string
  name: string
  image: string
  status: string
  created: string
  ports: ContainerPort[]
}

export interface RunningContainersResponse {
  count: number
  containers: RunningContainer[]
}

export interface ContainerResourceStatus {
  id: string
  name: string
  cpu_percent: number
  memory_usage_bytes: number
  memory_usage: string
  memory_limit_bytes: number
  memory_limit: string
  memory_percent: number
  network_received_bytes: number
  network_sent_bytes: number
  block_read_bytes: number
  block_write_bytes: number
  pids: number
  sampled_at: string
}

export interface ContainerStatusResponse {
  count: number
  total_cpu_percent: number
  total_memory_usage_bytes: number
  total_memory_usage: string
  total_network_received_bytes: number
  total_network_sent_bytes: number
  total_block_read_bytes: number
  total_block_write_bytes: number
  total_pids: number
  containers: ContainerResourceStatus[]
}

export function fetchContainers(
  signal?: AbortSignal,
): Promise<RunningContainersResponse> {
  return getJson<RunningContainersResponse>('/containers', signal)
}

export function fetchContainerStatus(
  signal?: AbortSignal,
): Promise<ContainerStatusResponse> {
  return getJson<ContainerStatusResponse>('/containers/status', signal)
}

/* --- All containers, detail, and destructive actions ------------------- */

export interface AllContainersResponse {
  count: number
  containers: RunningContainer[]
}

export interface ContainerMount {
  type: string
  name: string | null
  source: string
  destination: string
  driver: string
  mode: string
  read_write: boolean
}

export interface ContainerNetwork {
  name: string
  network_id: string
  endpoint_id: string
  gateway: string
  ip_address: string
  mac_address: string
}

export interface ContainerDetails {
  id: string
  short_id: string
  name: string
  image: string
  image_id: string
  status: string
  created: string
  started_at: string
  finished_at: string
  restart_count: number
  platform: string
  ports: ContainerPort[]
  mounts: ContainerMount[]
  networks: ContainerNetwork[]
  labels: Record<string, string>
}

export interface RemoveContainerResponse {
  id: string
  name: string
  removed: boolean
  removed_anonymous_volumes: boolean
}

export interface PruneContainersResponse {
  deleted: string[]
  reclaimed_bytes: number
  reclaimed: string
}

export interface StopContainerResponse {
  id: string
  name: string
  status: string
}

export interface ContainerFileDetails {
  path: string
  name: string
  size_bytes: number
  mode: string
  modified_at: string
  link_target: string
  encoding: string
  content: string
}

export interface ContainerFileResponse {
  container_id: string
  container_name: string
  file: ContainerFileDetails
}

export function fetchAllContainers(
  signal?: AbortSignal,
): Promise<AllContainersResponse> {
  return getJson<AllContainersResponse>('/containers/all', signal)
}

export function fetchContainerDetails(
  containerId: string,
  signal?: AbortSignal,
): Promise<ContainerDetails> {
  return getJson<ContainerDetails>(
    `/containers/${encodeURIComponent(containerId)}`,
    signal,
  )
}

export function fetchContainerFile(
  containerId: string,
  path: string,
  options: { maxBytes?: number } = {},
  signal?: AbortSignal,
): Promise<ContainerFileResponse> {
  const query = new URLSearchParams({ path })
  if (options.maxBytes) query.set('max_bytes', String(options.maxBytes))

  return getJson<ContainerFileResponse>(
    `/containers/${encodeURIComponent(containerId)}/files?${query}`,
    signal,
  )
}

/* The backend rejects each of these unless confirm=true is sent. */

export function removeContainer(
  containerId: string,
  options: { force?: boolean; removeVolumes?: boolean } = {},
): Promise<RemoveContainerResponse> {
  const query = new URLSearchParams({ confirm: 'true' })
  if (options.force) query.set('force', 'true')
  if (options.removeVolumes) query.set('remove_volumes', 'true')

  return deleteJson<RemoveContainerResponse>(
    `/containers/${encodeURIComponent(containerId)}?${query}`,
  )
}

export function pruneContainers(): Promise<PruneContainersResponse> {
  return postJson<PruneContainersResponse>('/containers/prune', {
    confirm: true,
  })
}

export function stopContainer(
  containerId: string,
  timeoutSeconds = 10,
): Promise<StopContainerResponse> {
  return postJson<StopContainerResponse>(
    `/containers/${encodeURIComponent(containerId)}/stop`,
    { confirm: true, timeout_seconds: timeoutSeconds },
  )
}

/* --- Processes and interactive shell ----------------------------------- */

/** One `docker top` sample. Every row in `processes` matches `titles` in
 *  length and order, and Docker decides which columns those are. */
export interface ContainerProcessesResponse {
  container_id: string
  container_name: string
  titles: string[]
  count: number
  processes: string[][]
}

export function fetchContainerProcesses(
  containerId: string,
  signal?: AbortSignal,
): Promise<ContainerProcessesResponse> {
  return getJson<ContainerProcessesResponse>(
    `/containers/${encodeURIComponent(containerId)}/processes`,
    signal,
  )
}

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** Absolute ws:// or wss:// URL for a fresh shell inside the container,
 *  behind the same `/api` prefix the REST calls use. */
export function containerShellSocketUrl(containerId: string): string {
  const url = new URL(
    `${API_BASE}/containers/${encodeURIComponent(containerId)}/shell`,
    window.location.href,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

/** Renders a port mapping the way `docker ps` does. */
export function formatPort(port: ContainerPort): string {
  const target = `${port.container_port}/${port.protocol}`
  if (port.host_port === null) return target
  const host = port.host_ip ? `${port.host_ip}:` : ''
  return `${host}${port.host_port} → ${target}`
}
