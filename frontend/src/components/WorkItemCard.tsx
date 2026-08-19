import { useState } from 'react'
import type { AgentProvider } from '../api/agents'
import type { DelegationView, ItemRouting, WorkItemState, WorkItemView } from '../api/delegation'
import CollapsibleCard from './CollapsibleCard'
import TurnConsole from './TurnConsole'

function stateLabel(state: WorkItemState): string {
  return {
    blocked: 'Blocked',
    ready: 'Ready',
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
  }[state]
}

function stateTone(state: WorkItemState): string {
  if (state === 'completed') return 'ok'
  if (state === 'failed') return 'err'
  if (state === 'blocked') return 'muted'
  return 'warn'
}

function money(value: number | null): string {
  return value === null ? 'not reported' : `$${value.toFixed(4)}`
}

function RoutingControls({
  routing,
  disabled,
  onSave,
  onClear,
}: {
  routing: ItemRouting
  disabled: boolean
  onSave: (provider: AgentProvider | null, model: string | null) => void
  onClear: () => void
}) {
  const [provider, setProvider] = useState<AgentProvider | ''>(
    routing.override_provider ?? '',
  )
  const [model, setModel] = useState(routing.override_model ?? '')
  // 'Custom…' keeps an unlisted model reachable. Without it the dropdown would
  // be a hard gate on a catalogue this deployment may simply not know yet.
  const [custom, setCustom] = useState(false)

  // Which provider's catalogue applies: the override if set, otherwise the one
  // routing already resolved.
  const effectiveProvider = provider || routing.provider
  const models = routing.models_by_provider?.[effectiveProvider] ?? []
  const recommended =
    routing.recommended_by_provider?.[effectiveProvider] ??
    routing.recommended_model
  const listed = models.includes(model)
  const showCustomField = custom || (model !== '' && !listed)

  // A model belongs to one provider, so changing provider drops a model that
  // provider cannot run rather than saving a mismatch.
  const changeProvider = (next: AgentProvider | '') => {
    setProvider(next)
    const nextProvider = next || routing.provider
    const nextModels = routing.models_by_provider?.[nextProvider] ?? []
    if (model && listed && !nextModels.includes(model)) {
      setModel('')
      setCustom(false)
    }
  }

  return (
    <div className="delegation-routing">
      <label>
        Provider override
        <select
          value={provider}
          onChange={(event) =>
            changeProvider(event.target.value as AgentProvider | '')
          }
          disabled={disabled}
        >
          <option value="">Automatic</option>
          <option value="claude">Claude</option>
          <option value="codex">Codex</option>
        </select>
      </label>
      <label>
        Model override
        <select
          value={showCustomField ? '__custom__' : model}
          onChange={(event) => {
            const next = event.target.value
            if (next === '__custom__') {
              setCustom(true)
              return
            }
            setCustom(false)
            setModel(next)
          }}
          disabled={disabled}
        >
          <option value="">
            Recommended{recommended ? ` (${recommended})` : ''}
          </option>
          {models.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
          <option value="__custom__">Custom…</option>
        </select>
        {/* Nested inside the label rather than added as a fourth grid child,
            which would take the button column and push the buttons to a row
            of their own. */}
        {showCustomField && (
          <input
            aria-label="Custom model"
            value={model}
            maxLength={100}
            placeholder={recommended}
            onChange={(event) => setModel(event.target.value)}
            disabled={disabled}
          />
        )}
      </label>
      <div className="button-row">
        <button
          type="button"
          className="small"
          disabled={disabled || (!provider && !model.trim())}
          onClick={() => onSave(provider || null, model.trim() || null)}
        >
          Save override
        </button>
        <button
          type="button"
          className="small ghost"
          disabled={disabled || (!routing.override_provider && !routing.override_model)}
          onClick={onClear}
        >
          Clear
        </button>
      </div>
    </div>
  )
}

/**
 * Why "Run item" is disabled, or null when it is not.
 *
 * A disabled button with no reason beside it is unreadable: the delegation
 * status that actually gates it lives at the top of the panel, and the
 * dependency that gates it lives in another card.
 */
function runBlockedReason(entry: WorkItemView, delegation: DelegationView): string | null {
  if (entry.state === 'running') return null
  if (entry.state === 'completed') return 'This work item is already complete.'
  if (entry.state === 'blocked') {
    return `Blocked by ${entry.blocked_by.join(', ') || 'an upstream work item'}.`
  }
  const status = delegation.delegation.status
  if (status === 'halted') {
    return 'The delegation is halted. Resume it above to run work items again.'
  }
  if (status === 'completed') return 'The delegation is already complete.'
  if (status === 'abandoned') return 'The delegation was abandoned.'
  return null
}

export function ItemCard({
  entry,
  delegation,
  busy,
  watching,
  projectName,
  sessionId,
  onReload,
  onRun,
  onSetRouting,
  onClearRouting,
}: {
  entry: WorkItemView
  delegation: DelegationView
  busy: string
  /** Run id the workspace is streaming, so only that card shows a console. */
  watching: string | null
  projectName: string
  sessionId: string
  onReload: () => void
  onRun: () => void
  onSetRouting: (provider: AgentProvider | null, model: string | null) => void
  onClearRouting: () => void
}) {
  const latest = entry.runs[entry.runs.length - 1]
  const canRun =
    ['ready', 'failed'].includes(entry.state) &&
    ['ready', 'running'].includes(delegation.delegation.status)
  const blockedReason = runBlockedReason(entry, delegation)
  const verificationPassed = latest?.verification?.passed === true
  const isWatched = watching !== null && latest?.id === watching
  const isBusy = Boolean(busy)

  return (
    <CollapsibleCard
      title={entry.item.title}
      defaultOpen={entry.state === 'ready' || entry.state === 'running'}
      aside={
        <>
          <span className={`pill ${stateTone(entry.state)}`}>
            {stateLabel(entry.state)}
          </span>
          <span className="pill muted">{entry.item.complexity}</span>
        </>
      }
    >
      <p>{entry.item.objective}</p>
      <ul className="kv-rows">
        <li>
          <span className="kv-key">Scope</span>
          <span className="kv-value">{entry.item.scope}</span>
        </li>
        <li>
          <span className="kv-key">Dependencies</span>
          <span className="kv-value">
            {entry.item.dependencies.join(', ') || 'None'}
          </span>
        </li>
        <li>
          <span className="kv-key">Blocked by</span>
          <span className="kv-value">{entry.blocked_by.join(', ') || 'Nobody'}</span>
        </li>
        <li>
          <span className="kv-key">Can run in parallel</span>
          <span className="kv-value">
            {entry.can_run_in_parallel_with.join(', ') || 'No other item'}
          </span>
        </li>
        <li>
          <span className="kv-key">Files</span>
          <span className="kv-value mono">{entry.item.files.join(', ') || 'Not named'}</span>
        </li>
      </ul>

      <div className="section-heading">Acceptance criteria</div>
      <ul>
        {entry.item.acceptance_criteria.map((criterion) => (
          <li key={criterion}>{criterion}</li>
        ))}
      </ul>

      {entry.routing && (
        <>
          <div className="section-heading">Routing</div>
          <p className="status">
            Recommended: <span className="mono">{entry.routing.recommended_model}</span>.
            Chosen: <span className="mono">{entry.routing.provider}/{entry.routing.model}</span>
            {' '}via {entry.routing.source.replaceAll('_', ' ')}.
          </p>
          {entry.routing.warning && (
            <p className="status status-warning">{entry.routing.warning}</p>
          )}
          <RoutingControls
            key={`${entry.routing.override_provider}-${entry.routing.override_model}`}
            routing={entry.routing}
            disabled={isBusy || entry.state === 'running' || entry.state === 'completed'}
            onSave={onSetRouting}
            onClear={onClearRouting}
          />
        </>
      )}

      {latest && (
        <div className="delegation-run-summary">
          <div className="section-heading">Latest attempt</div>
          <div className="detail-status-row">
            <span className={`pill ${latest.status === 'succeeded' ? 'ok' : latest.status === 'failed' ? 'err' : 'warn'}`}>
              {latest.status}
            </span>
            <span className="pill muted">attempt {latest.attempt}</span>
            <span className={`pill ${verificationPassed ? 'ok' : 'muted'}`}>
              Verification {verificationPassed ? 'passed' : 'not passed'}
            </span>
            {latest.repair_count > 0 && (
              <span className="pill warn">{latest.repair_count} repair</span>
            )}
          </div>
          <p className="status">
            {latest.provider}/{latest.model ?? 'model not reported'} · {money(latest.usage.cost_usd)}
            {latest.duration_ms === null ? '' : ` · ${latest.duration_ms} ms`}
          </p>
          {latest.error && <p className="status status-error">{latest.error}</p>}
        </div>
      )}

      {isWatched && (
        <TurnConsole
          projectName={projectName}
          sessionId={sessionId}
          kind="run"
          jobId={watching}
          title={`${entry.item.title} — ${latest?.provider ?? 'model'}/${latest?.model ?? ''}`}
          onFinished={onReload}
        />
      )}

      {!canRun && blockedReason && <p className="status">{blockedReason}</p>}

      <div className="button-row delegation-item-actions">
        <button
          type="button"
          className="primary"
          disabled={isBusy || !canRun}
          title={canRun ? undefined : blockedReason ?? undefined}
          onClick={onRun}
        >
          {entry.state === 'running' ? 'Running…' : 'Run item'}
        </button>
      </div>
    </CollapsibleCard>
  )
}
