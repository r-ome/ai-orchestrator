import { getJson, postJson } from './client'

export type AgentProvider = 'claude' | 'codex'

export interface AgentProviderDetails {
  provider: AgentProvider
  image: string
  command: string[]
  credential_directory: string
  credential_environment_variable: string
}

export interface AgentProvidersResponse {
  providers: AgentProviderDetails[]
}

export interface CodingAgent {
  id: string
  run_id: string
  sandbox_id: string
  short_id: string
  name: string
  provider: AgentProvider
  image: string
  command: string[]
  status: string
  created_at: string
  project_name: string
  project_volume: string
  credential_profile: string
  credential_volume: string
  workspace: string
  /** Backend-relative path, e.g. `/agents/<id>/ws`. See `agentSocketUrl`. */
  websocket_url: string
}

export interface CodingAgentsResponse {
  count: number
  agents: CodingAgent[]
}

export interface StopAgentResponse {
  id: string
  name: string
  stopped: boolean
}

/** Mirrors the backend's `CreateAgentRequest.credential_profile` pattern. */
export const CREDENTIAL_PROFILE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
export const CREDENTIAL_PROFILE_MAX_LENGTH = 64

export function fetchAgentProviders(
  signal?: AbortSignal,
): Promise<AgentProvidersResponse> {
  return getJson<AgentProvidersResponse>('/agents/providers', signal)
}

export function fetchAgents(signal?: AbortSignal): Promise<CodingAgentsResponse> {
  return getJson<CodingAgentsResponse>('/agents', signal)
}

export function fetchAgent(
  agentId: string,
  signal?: AbortSignal,
): Promise<CodingAgent> {
  return getJson<CodingAgent>(`/agents/${encodeURIComponent(agentId)}`, signal)
}

/** Creates and starts an idle agent container. The first terminal WebSocket
 *  starts the CLI in tmux. Later sockets reattach to that session. */
export function summonAgent(
  projectName: string,
  provider: AgentProvider,
  credentialProfile: string,
): Promise<CodingAgent> {
  return postJson<CodingAgent>('/agents', {
    project_name: projectName,
    provider,
    credential_profile: credentialProfile,
  })
}

export function stopAgent(
  agentId: string,
  timeoutSeconds = 2,
): Promise<StopAgentResponse> {
  return postJson<StopAgentResponse>(
    `/agents/${encodeURIComponent(agentId)}/stop`,
    { confirm: true, timeout_seconds: timeoutSeconds },
  )
}

export function replaceAgent(
  agentId: string,
  provider: AgentProvider,
  credentialProfile: string,
  timeoutSeconds = 2,
): Promise<CodingAgent> {
  return postJson<CodingAgent>(
    `/agents/${encodeURIComponent(agentId)}/replace`,
    {
      provider,
      credential_profile: credentialProfile,
      confirm: true,
      timeout_seconds: timeoutSeconds,
    },
  )
}

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** Turns the backend-relative `websocket_url` into an absolute ws:// or
 *  wss:// URL behind the same `/api` prefix the REST calls use. */
export function agentSocketUrl(agent: CodingAgent): string {
  const url = new URL(`${API_BASE}${agent.websocket_url}`, window.location.href)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}
