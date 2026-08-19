export interface PhaseAgent {
  id: string
  role: 'clarifier' | 'planner' | 'reviewer' | 'work-item'
  label: string
  detail: string
  provider: string | null
  model: string | null
  reasoningEffort?: string | null
  state: 'active' | 'done' | 'pending'
}
