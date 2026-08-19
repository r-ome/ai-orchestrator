import type {
  PlanningMessage,
  PlanningSession,
  PlanningSessionDetail,
  PlanningStatus,
} from '../api/planning'
import type { DelegationTabId } from '../components/DelegationWorkspace'

export type PendingDialog = 'proceed' | 'cancel' | null

/**
 * Every phase of the session, planning and delegation alike, in one sidebar.
 *
 * The delegation phases were their own page. They are the same session though:
 * a plan is only worth reading next to what is being built from it, and the
 * reader who approves a plan is the reader who then runs its work items.
 */
export type TabId = 'clarifier' | 'review' | 'spec' | DelegationTabId | 'preview'

export function thinkingRole(status: PlanningStatus): string | null {
  if (status === 'clarifying' || status === 'awaiting_confirmation') {
    return 'Clarifier'
  }
  if (status === 'planning') return 'Planner'
  if (status === 'under_review') return 'Plan reviewer'
  return null
}

/**
 * The tab the session's current phase makes most useful.
 *
 * Used only until the reader picks a tab themselves, so an open page follows a
 * running session from clarification through to the finished spec.
 */
export function phaseTab(status: PlanningStatus): TabId {
  if (status === 'plan_ready' || status === 'review_limit_reached') return 'spec'
  if (status === 'planning' || status === 'under_review') return 'review'
  return 'clarifier'
}

export interface SplitMessages {
  clarifier: PlanningMessage[]
  review: PlanningMessage[]
}

/**
 * Splits the message log into the clarification thread and the planning thread.
 *
 * A system message belongs to whichever phase produced it, so it lands in the
 * clarification thread until the first planner turn appears and in the planning
 * thread after that. Otherwise a clarifier failure would be recorded on a tab
 * the reader has no reason to open.
 */
export function splitMessages(messages: PlanningMessage[]): SplitMessages {
  const ordered = [...messages].sort((left, right) => left.sequence - right.sequence)
  const firstPlanner = ordered.find((entry) => entry.role === 'planner')
  const planningStarts = firstPlanner?.sequence ?? Number.POSITIVE_INFINITY

  const split: SplitMessages = { clarifier: [], review: [] }
  for (const entry of ordered) {
    if (entry.role === 'user' || entry.role === 'clarifier') {
      split.clarifier.push(entry)
    } else if (entry.role === 'planner' || entry.role === 'reviewer') {
      split.review.push(entry)
    } else if (entry.sequence < planningStarts) {
      split.clarifier.push(entry)
    } else {
      split.review.push(entry)
    }
  }
  return split
}

/** One planner revision and the reviewer round that answered it. */
export interface ReviewRound {
  key: string
  number: number
  planner: PlanningMessage | null
  reviewer: PlanningMessage | null
  /** System turns, and any second reviewer turn, recorded inside this round. */
  extra: PlanningMessage[]
}

export interface GroupedReview {
  /** Turns recorded before the planner's first revision, so before round one. */
  preamble: PlanningMessage[]
  rounds: ReviewRound[]
}

/**
 * Groups the planning thread into rounds, one per planner revision.
 *
 * A round is the unit the loop actually runs in: the planner writes a revision,
 * the reviewer answers it once, and the two either settle or go again. Grouping
 * on the planner turn rather than on the revision number keeps a round intact
 * even when a turn arrives without one.
 */
export function groupRounds(messages: PlanningMessage[]): GroupedReview {
  const grouped: GroupedReview = { preamble: [], rounds: [] }

  for (const entry of messages) {
    if (entry.role === 'planner') {
      grouped.rounds.push({
        key: `round-${entry.sequence}`,
        number: entry.revision ?? grouped.rounds.length + 1,
        planner: entry,
        reviewer: null,
        extra: [],
      })
      continue
    }

    const current = grouped.rounds[grouped.rounds.length - 1]
    if (!current) {
      grouped.preamble.push(entry)
    } else if (entry.role === 'reviewer' && current.reviewer === null) {
      current.reviewer = entry
    } else {
      current.extra.push(entry)
    }
  }

  return grouped
}

export function roundVerdict(round: ReviewRound): { label: string; tone: string } {
  if (round.reviewer === null || round.reviewer.approved === null) {
    return { label: 'Awaiting review', tone: 'muted' }
  }
  return round.reviewer.approved
    ? { label: 'Approved', tone: 'ok' }
    : { label: 'Changes requested', tone: 'warn' }
}

export function providerFor(
  session: PlanningSessionDetail,
  message: PlanningMessage,
): PlanningSessionDetail['clarifier_provider'] {
  if (message.role === 'planner') return session.planner_provider
  if (message.role === 'reviewer') return session.reviewer_provider
  return session.clarifier_provider
}

export function sessionStatusLine(session: PlanningSession): string {
  if (session.feature_status === 'building') {
    return `building · ${session.review_turn}/${session.max_review_turns}`
  }
  if (session.status === 'under_review') return `under review · rev ${session.plan_revision}`
  if (session.status === 'plan_ready') return 'plan ready'
  if (session.status === 'planning') return `planning · rev ${session.plan_revision}`
  if (session.status === 'review_limit_reached') return 'review limit reached'
  if (session.status === 'awaiting_confirmation') return 'awaiting confirmation'
  return session.status
}
