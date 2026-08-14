import type { AgentProvider } from './agents'
import { getJson, postJson } from './client'

export type PlanningStatus =
  | 'clarifying'
  | 'awaiting_confirmation'
  | 'planning'
  | 'under_review'
  | 'plan_ready'
  | 'review_limit_reached'
  | 'failed'
  | 'cancelled'

export type FeatureStatus =
  | 'clarifying'
  | 'awaiting_confirmation'
  | 'planning'
  | 'under_review'
  | 'plan_ready'
  | 'building'
  | 'blocked'
  | 'in_review'
  | 'approved'
  | 'published'
  | 'merged'
  | 'abandoned'
  | 'review_limit_reached'
  | 'failed'
  | 'cancelled'

export type PlanningTurnState = 'idle' | 'running'

export type PlanningRole = 'user' | 'clarifier' | 'planner' | 'reviewer' | 'system'

export type FindingStatus = 'open' | 'answered' | 'rejected' | 'resolved'

export interface CreatePlanningSessionBody {
  title: string
  request: string
  clarifier_provider?: AgentProvider
  planner_provider?: AgentProvider
  reviewer_provider?: AgentProvider
  clarifier_model?: string
  planner_model?: string
  reviewer_model?: string
  reviewer_reasoning_effort?: string
  max_review_turns?: number
}

export interface PlanningDefaults {
  clarifier_provider: AgentProvider
  planner_provider: AgentProvider
  reviewer_provider: AgentProvider
  claude_model: string
  codex_model: string
  codex_reasoning_effort: string
  max_review_turns: number
  models_by_provider: Record<string, string[]>
  reasoning_efforts: string[]
}

/** A finding as one reviewer round raised it, with no ledger status. */
export interface PlanningMessageFinding {
  finding_id: string
  severity: string
  text: string
}

export interface PlanningMessageFindingResponse {
  finding_id: string
  status: string
  rationale: string
}

export interface PlanningMessage {
  sequence: number
  role: PlanningRole
  text: string
  questions: string[]
  revision: number | null
  approved: boolean | null
  findings: PlanningMessageFinding[]
  finding_responses: PlanningMessageFindingResponse[]
  /** The raw log is behind its own endpoint, so this only says whether one exists. */
  has_raw_output: boolean
  /** The model that ran this turn, recorded when it ran. Empty for a human turn. */
  model: string
  created_at: string
}

export interface PlanningMessageRaw {
  sequence: number
  role: PlanningRole
  raw_output: string
}

export interface PlanningFinding {
  finding_id: string
  severity: string
  text: string
  status: FindingStatus
  planner_response: string
  raised_in_round: number
  last_seen_round: number
}

export interface PlanComponent {
  name: string
  responsibility: string
}

export interface PlanRisk {
  severity: string
  text: string
}

export interface ReviewerOutcome {
  approved: boolean
  rounds: number
  summary: string
  outstanding_findings: PlanningFinding[]
}

export interface PlanSpec {
  title: string
  scope: string
  approach: string
  components: PlanComponent[]
  risks: PlanRisk[]
  open_questions: string[]
  reviewer_outcome: ReviewerOutcome
  plan_markdown: string
  confirmed_understanding: boolean
  generated_at: string
}

export interface PlanningSession {
  id: string
  project_id: string
  project_name: string
  sandbox_id: string
  title: string
  status: PlanningStatus
  feature_status: FeatureStatus
  turn_state: PlanningTurnState
  clarifier_provider: AgentProvider
  planner_provider: AgentProvider
  reviewer_provider: AgentProvider
  clarifier_model: string | null
  planner_model: string | null
  reviewer_model: string | null
  reviewer_reasoning_effort: string | null
  max_review_turns: number
  review_turn: number
  plan_revision: number
  confirmed: boolean
  understanding_summary: string
  failure_reason: string
  created_at: string
  updated_at: string
  settled_at: string | null
}

export interface PlanningSessionDetail extends PlanningSession {
  feature_brief: string
  messages: PlanningMessage[]
  findings: PlanningFinding[]
  plan_spec: PlanSpec | null
}

export interface PlanningSessionsResponse {
  count: number
  sessions: PlanningSession[]
}

export const PLANNING_TERMINAL_STATUSES = new Set<PlanningStatus>([
  'plan_ready',
  'review_limit_reached',
  'failed',
  'cancelled',
])

export function isPlanningTerminal(status: PlanningStatus): boolean {
  return PLANNING_TERMINAL_STATUSES.has(status)
}

function planningPath(projectName: string): string {
  return `/projects/${encodeURIComponent(projectName)}/planning`
}

export function fetchPlanningSessions(
  projectName: string,
  signal?: AbortSignal,
): Promise<PlanningSessionsResponse> {
  return getJson<PlanningSessionsResponse>(
    `${planningPath(projectName)}/sessions`,
    signal,
  )
}

export function fetchPlanningDefaults(
  projectName: string,
  signal?: AbortSignal,
): Promise<PlanningDefaults> {
  return getJson<PlanningDefaults>(`${planningPath(projectName)}/defaults`, signal)
}

export function fetchPlanningSession(
  projectName: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<PlanningSessionDetail> {
  return getJson<PlanningSessionDetail>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}`,
    signal,
  )
}

export function fetchPlanningMessageRaw(
  projectName: string,
  sessionId: string,
  sequence: number,
  signal?: AbortSignal,
): Promise<PlanningMessageRaw> {
  return getJson<PlanningMessageRaw>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}` +
      `/messages/${sequence}/raw`,
    signal,
  )
}

export function createPlanningSession(
  projectName: string,
  body: CreatePlanningSessionBody,
): Promise<PlanningSession> {
  return postJson<PlanningSession>(`${planningPath(projectName)}/sessions`, body)
}

export function sendPlanningMessage(
  projectName: string,
  sessionId: string,
  text: string,
): Promise<PlanningSession> {
  return postJson<PlanningSession>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}/messages`,
    { text },
  )
}

export function confirmPlanningUnderstanding(
  projectName: string,
  sessionId: string,
): Promise<PlanningSession> {
  return postJson<PlanningSession>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}/confirm`,
  )
}

export function correctPlanningUnderstanding(
  projectName: string,
  sessionId: string,
  text: string,
): Promise<PlanningSession> {
  return postJson<PlanningSession>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}/correct`,
    { text },
  )
}

export function proceedPlanningSession(
  projectName: string,
  sessionId: string,
): Promise<PlanningSession> {
  return postJson<PlanningSession>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}/proceed`,
  )
}

export function cancelPlanningSession(
  projectName: string,
  sessionId: string,
): Promise<PlanningSession> {
  return postJson<PlanningSession>(
    `${planningPath(projectName)}/sessions/${encodeURIComponent(sessionId)}/cancel`,
  )
}
