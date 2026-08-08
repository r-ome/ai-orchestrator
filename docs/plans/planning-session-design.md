# Design spec: project-level planning sessions

Companion to `docs/adr/0004-controller-owned-planning-sessions.md`.
Phase order and exit criteria live in `planning-session-plan.md`.
Implementation detail lives in `planning-session-spec.md`.

This document defines behaviour: the language, the states, what each actor
sees, and what every contract between the controller and a model must contain.
It does not name line numbers. When behaviour here and detail in the tech spec
disagree, this document wins.

---

## 1. Language

Added to the project vocabulary in `CONTEXT.md`. Follow the same rule as the
existing entries: one word, one meaning.

**Planning session**
: One attempt to turn a feature request into a reviewed plan. It holds a
  conversation, planner and reviewer output, a status, and at most one Plan
  Spec. It belongs to a project, not to a sandbox.
  _Avoid_: planning run, plan thread.

**Clarifier**
: The model that questions the human until the feature is understood. It never
  writes a plan.
  _Avoid_: main model, interviewer.

**Planner**
: The model that turns the agreed brief into a proposed implementation plan. Its
  conversation persists across review rounds.

**Plan reviewer**
: The model that judges one plan revision. A fresh plan reviewer runs for each
  revision.
  _Avoid_: reviewer, critic, checker. `CONTEXT.md` already gives **Reviewer** to
  the read-only sandbox participant, so the bare word is taken. This document
  says *reviewer* only inside the planning flow, where no other reviewer exists.

**Feature brief**
: The controller-assembled document sent to the planner. Frozen at the moment
  the human confirms. It contains the title, the original request, the confirmed
  understanding, and the clarification question-and-answer pairs.
  _Avoid_: requirements, prompt.

**Plan revision**
: One planner output. Numbered from 1.

**Finding**
: One reviewer objection to a plan revision. It carries a controller-assigned
  stable id, a severity, a status, and the planner's response.

**Review ledger**
: The compact, per-session record of every unresolved finding and its current
  state. It is what a renewed reviewer receives instead of the review history.

**Plan Spec**
: The final document of a planning session: agreed scope, proposed approach,
  major components, risks, open questions, and reviewer outcome. Produced on
  approval and at the review limit. Never produced for a cancelled or failed
  session.

**Turn**
: One model invocation. One short-lived container. A session runs at most one
  turn at a time.

---

## 2. States

### Session status

| Status | Meaning | Terminal |
|---|---|---|
| `clarifying` | The clarifier is asking questions, or is waiting for an answer | no |
| `awaiting_confirmation` | The clarifier has stated its understanding; the human must confirm or correct it | no |
| `planning` | The planner is producing a revision | no |
| `under_review` | A reviewer is judging the current revision | no |
| `plan_ready` | The reviewer approved. A Plan Spec exists | yes |
| `review_limit_reached` | The round limit was hit without approval. A Plan Spec exists, marked not approved | yes |
| `failed` | A turn could not be completed | yes |
| `cancelled` | A human ended the session | yes |

Permitted transitions. Anything absent is unreachable.

| From | To | Trigger |
|---|---|---|
| `clarifying` | `awaiting_confirmation` | A clarifier turn reports it is ready to summarise |
| `clarifying` | `planning` | The human presses **Proceed anyway** |
| `clarifying` | `failed`, `cancelled` | Turn failure, human cancel |
| `awaiting_confirmation` | `clarifying` | The human presses **Correct** and sends a correction |
| `awaiting_confirmation` | `planning` | The human presses **Confirm** |
| `awaiting_confirmation` | `failed`, `cancelled` | Turn failure, human cancel |
| `planning` | `under_review` | A planner revision was written |
| `planning` | `failed`, `cancelled` | Turn failure, human cancel |
| `under_review` | `plan_ready` | The reviewer approved |
| `under_review` | `planning` | The reviewer rejected and rounds remain |
| `under_review` | `review_limit_reached` | The reviewer rejected and the limit is reached |
| `under_review` | `failed`, `cancelled` | Turn failure, human cancel |

`cancelled` is reachable from every non-terminal status. Terminal statuses have
no exit.

### Turn state

`turn_state` is `idle` or `running`, and is tracked separately from status. It
answers "is a container working right now", which status cannot: a session is
`clarifying` both while a model thinks and while a human types.

Only one turn runs per session. A second start attempt is refused, not queued.

### Finding status

| Status | Meaning |
|---|---|
| `open` | Raised by a reviewer, not yet answered by the planner |
| `answered` | The planner claims to have addressed it, with a rationale |
| `rejected` | The planner declined it, with a rationale |
| `resolved` | A later reviewer saw it in the ledger and did not re-raise it |

`resolved` findings leave the ledger. They stay in the session record for audit.

---

## 3. Flow

### 3.1 Starting

The human presses **Plan a feature** on the project page. A dialog takes:

- a short title, required;
- the feature request in free text, required;
- optional provider overrides for clarifier, planner, and reviewer;
- an optional review-round limit.

Creating the session stores the request as the first message, sets `clarifying`,
and starts one clarifier turn. There is no empty opening turn: the clarifier's
first output is a response to a request the human has already written and can
already see on screen.

### 3.2 Clarifying

Each clarifier turn receives the full conversation and the read-only project
volume. It returns a short message, at most three questions, and a signal for
whether it is ready to summarise.

The clarifier must question before it plans. Its instructions require it to
cover, across the conversation and not in one turn:

- the outcome the human wants, in the human's own terms;
- what is in scope and what is explicitly out;
- constraints: existing systems, conventions, data, deadlines;
- expected behaviour, including error and edge behaviour;
- the trade-offs that matter and which side the human takes.

Progressive means at most three questions per turn, chosen because the previous
answer made them the next most useful ones. A questionnaire is a defect.

The human answers in the composer. Each answer starts one turn.

### 3.3 Agreeing

When the clarifier signals readiness, it must also supply its understanding
summary. The session moves to `awaiting_confirmation` and the summary is shown
apart from the conversation, as a claim awaiting a decision.

The human has three actions, available as follows:

| Action | Available when | Effect |
|---|---|---|
| **Confirm** | `awaiting_confirmation` | Freezes the feature brief; session moves to `planning` |
| **Correct** | `awaiting_confirmation` | The human sends a correction; session returns to `clarifying` |
| **Proceed anyway** | `clarifying` or `awaiting_confirmation`, turn idle | Freezes a brief from what exists; session moves to `planning` |

**Proceed anyway** exists so the human can stop the questioning at any point. It
is not hidden behind readiness. When it is used from `clarifying`, there is no
confirmed understanding, and the brief records that: the Plan Spec's scope
section carries the note that the human proceeded without confirming a summary.

Agreement is never inferred from the model's signal alone. The signal changes
what the screen offers; a human click changes the phase.

### 3.4 Planning and review

The controller sends the frozen brief to the planner. The planner returns a plan
revision and, from round 2 onward, a response to every finding in the ledger.

The controller sends the revision to a fresh reviewer along with the brief and
the ledger. The reviewer returns a verdict and findings.

- Approved: the session becomes `plan_ready` and a Plan Spec is written.
- Rejected with rounds remaining: the ledger is updated and the planner runs
  again. Its conversation continues; it sees its own previous plan.
- Rejected at the limit: the session becomes `review_limit_reached` and a Plan
  Spec is still written, marked not approved and carrying the outstanding
  findings.

The reviewer is renewed each revision so it judges the plan in front of it, not
the argument that produced it. The ledger is what stops it repeating itself.
The ledger is explicitly context and not truth. The reviewer's instructions
require it to:

- assess the current plan from scratch;
- avoid reopening a finding marked `answered` unless the current plan gives a
  concrete reason to;
- accept a `rejected` finding's rationale or say precisely why the rationale
  does not hold;
- name every remaining issue and every newly introduced one.

Round counting: `review_turn` counts completed reviewer runs. The loop ends when
`review_turn` reaches `max_review_turns` without approval.

### 3.5 Ending

`plan_ready` and `review_limit_reached` both leave a Plan Spec. `cancelled` and
`failed` leave the conversation and any partial output for reading, and no Plan
Spec.

Nothing downstream is triggered. No task, no branch, no agent, no repository
write. The session's last act is to put a document on screen.

---

## 4. Model contracts

Every turn must return one JSON object and nothing else. The controller extracts
the first balanced JSON object from the model's final message, so surrounding
prose does not break a turn, but the instructions forbid it.

A malformed or schema-invalid payload gets exactly one repair turn, which
resends the original prompt plus the invalid output and the parse error. A
second failure fails the session with the raw output recorded.

### 4.1 Clarifier

```json
{
  "message": "one short paragraph to the human",
  "questions": ["at most three, each one sentence"],
  "ready_to_summarize": false,
  "understanding_summary": ""
}
```

Rules the controller enforces:

- `questions` holds at most 3 entries.
- When `ready_to_summarize` is `true`, `understanding_summary` must be
  non-empty and `questions` must be empty.
- When `ready_to_summarize` is `false`, `questions` must be non-empty.

A payload that breaks a rule is treated as malformed and gets the repair turn.

### 4.2 Planner

```json
{
  "plan_markdown": "the full plan as markdown",
  "scope": "what this feature includes and excludes",
  "approach": "the proposed approach in prose",
  "components": [{"name": "...", "responsibility": "..."}],
  "risks": [{"severity": "high|medium|low", "text": "..."}],
  "open_questions": ["..."],
  "finding_responses": [
    {"finding_id": "F1", "status": "answered|rejected", "rationale": "..."}
  ]
}
```

Rules:

- `plan_markdown`, `scope`, and `approach` are required and non-empty.
- `finding_responses` is empty on round 1 and must answer every ledger finding
  with status `open`, `answered`, or `rejected` on later rounds. A missing
  response is treated as still `open` rather than as an error, so one lazy
  turn does not fail a session.
- `finding_id` values that are not in the ledger are discarded.

### 4.3 Reviewer

```json
{
  "approved": false,
  "summary": "one paragraph verdict",
  "findings": [
    {"id": "F1", "severity": "blocking|major|minor", "text": "..."}
  ]
}
```

Rules:

- `approved: true` requires `findings` to contain no `blocking` or `major`
  entry. A model that approves while raising a blocking finding is corrected by
  the controller, which treats the verdict as a rejection and records why.
- `id` must reuse a ledger id when the reviewer is re-raising a known finding,
  and must be `NEW-1`, `NEW-2`, … for new ones. The controller mints stable ids
  for `NEW-*` and rewrites them. Model-chosen ids are never persisted directly,
  because a renewed reviewer would otherwise renumber every round.

### 4.4 Ledger sent to the reviewer

Structured, replaced each round, never appended:

```json
[
  {
    "id": "F1",
    "severity": "major",
    "text": "the original finding",
    "status": "answered",
    "planner_response": "how the planner says it was addressed",
    "raised_in_round": 1
  }
]
```

Only findings with status `open`, `answered`, or `rejected` are sent. `resolved`
findings are omitted.

---

## 5. Screens

### 5.1 Project page section

A **Planning** card, above the preview section, matching the existing section
components in shape and class names.

- Header: `Planning`, with a **Plan a feature** primary button. Disabled while
  the project is not ready, with the reason stated, exactly as the agents
  section already does.
- Body: the project's sessions, newest first. Each row shows title, status pill,
  provider trio, review round when relevant, and relative created time. The row
  links to the session page.
- Empty state: one sentence explaining that planning produces a reviewed plan
  and changes nothing in the project.

Status pill colours reuse the existing `pill` classes: `ok` for `plan_ready`,
`warn` for `awaiting_confirmation` and `review_limit_reached`, `danger` for
`failed`, `muted` for `cancelled`, default for the working statuses.

### 5.2 Session page

Route `/projects/:projectName/plans/:sessionId`, breadcrumbed back to the
project, matching the agent terminal page's shape.

Regions, top to bottom:

1. **Header.** Title, status pill, and, while a turn runs, a "thinking" marker
   naming the active role. **Cancel session** sits here, disabled on terminal
   statuses.
2. **Conversation.** The transcript, oldest first, with the role shown for each
   message. Clarifier questions render as a list. Planner and reviewer messages
   are labelled with their round.
3. **Understanding panel.** Shown only in `awaiting_confirmation`. Renders the
   summary, then **Confirm** and **Correct**.
4. **Composer.** Shown in `clarifying` and after **Correct**. Disabled while a
   turn runs, with the reason shown. Carries **Proceed anyway** beside send.
5. **Review progress.** Shown from `planning` onward: round *n* of *max*, the
   current revision number, and the open findings count.
6. **Plan Spec.** Shown on `plan_ready` and `review_limit_reached`. A concise
   summary first — scope, approach, component names, top risks, reviewer
   outcome — then the full markdown document behind a disclosure. When the
   outcome is not approved, a warning line states that the plan was not
   approved and lists the outstanding findings.

The page polls while the session is non-terminal and stops polling once it
settles.

### 5.3 What the human can always tell

Three questions the screen must answer without a click, in every state: what
phase is this in, is something running right now, and what can I do next. The
status pill answers the first, the thinking marker the second, and the enabled
actions the third.

---

## 6. Failure behaviour

| Failure | Result |
|---|---|
| Malformed JSON, twice | `failed`, `failure_reason` names the role and the parse error, raw output kept |
| Container exits non-zero | `failed`, `failure_reason` carries the trimmed stderr tail |
| Turn exceeds the timeout | `failed`, `failure_reason` says which role timed out |
| Backend restarts mid-turn | The session is reconciled to `failed`, never left `running` |
| Human cancels mid-turn | `cancelled` immediately; the turn's output is discarded on arrival |
| Message posted while a turn runs | 409, session unchanged |
| Confirm or proceed on a terminal session | 409, session unchanged |
| Provider not logged in | The CLI's own error surfaces as a `failed` session with its message |

A failed session is readable. It keeps its conversation and any planner or
reviewer output already stored.
