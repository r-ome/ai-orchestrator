import {
  clearItemRouting,
  driveDelegation,
  generateDelegation,
  resumeDelegation,
  setItemRouting,
  startWorkItem,
  type ContextManifest,
  type DelegationView,
} from '../api/delegation'
import CollapsibleCard from './CollapsibleCard'
import type { DelegationWorkspace } from './DelegationWorkspace'
import { ItemCard } from './WorkItemCard'
import TurnConsole from './TurnConsole'

function ContextManifestView({ manifest }: { manifest: ContextManifest | null }) {
  if (manifest === null) {
    return <p className="status">This context recorded no manifest.</p>
  }

  const notes: [string, string[]][] = [
    ['Architecture', manifest.architecture],
    ['Patterns', manifest.patterns],
    ['Constraints', manifest.constraints],
    ['Assumptions', manifest.assumptions],
  ]

  return (
    <>
      <div className="section-heading">Modules</div>
      {manifest.modules.length > 0 ? (
        <ul className="kv-rows">
          {manifest.modules.map((module) => (
            <li key={module.path}>
              <span className="kv-key mono">{module.path}</span>
              <span className="kv-value">{module.purpose}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="status">No modules were reported.</p>
      )}

      <div className="section-heading">Symbols</div>
      {manifest.symbols.length > 0 ? (
        <ul className="kv-rows">
          {manifest.symbols.map((symbol) => (
            <li key={`${symbol.location}:${symbol.name}`}>
              <span className="kv-key mono">{symbol.name}</span>
              <span className="kv-value">
                <span className="mono">{symbol.location}</span> — {symbol.role}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="status">No symbols were reported.</p>
      )}

      {notes.map(([heading, lines]) =>
        lines.length > 0 ? (
          <div key={heading}>
            <div className="section-heading">{heading}</div>
            <ul>
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
    </>
  )
}

/** Whether an unattended run has anything to do right now.
 *
 *  The backend refuses a drive on any other status, so this hides a control
 *  that would only produce a 409. A halted delegation is deliberately excluded:
 *  the resume card below is the correct next step, and offering both would
 *  invite restarting the run without reading why it stopped. */
function driveReady(delegation: DelegationView): boolean {
  const status = delegation.delegation.status
  if (status !== 'ready' && status !== 'running') return false
  return delegation.ready.length > 0
}

export function WorkItemsPanel({ workspace }: { workspace: DelegationWorkspace }) {
  const {
    projectName,
    sessionId,
    context,
    delegation,
    generatingContext,
    reload,
    busy,
    watching,
    openContextModal,
    runAction,
    watchTurn,
    clearWatch,
  } = workspace

  return (
    <>
      <CollapsibleCard
        title="Implementation context"
        // Collapsed by default: once it is ready, the work items below are
        // what the reader came for. It opens to answer "which commands is
        // verification actually going to run?".
        defaultOpen={false}
        aside={
          generatingContext ? (
            <span className="pill warn">
              generating
            </span>
          ) : context ? (
            <span className="pill ok">ready</span>
          ) : (
            <span className="pill muted">none</span>
          )
        }
      >
        <p>
          Repository pointers and controller-confirmed commands. Decomposition
          needs one before it can name files or verification.
        </p>
        {context ? (
          <>
            <ul className="kv-rows">
              <li>
                <span className="kv-key">Provider</span>
                <span className="kv-value mono">{context.provider ?? 'not reported'}</span>
              </li>
              <li>
                <span className="kv-key">Model</span>
                <span className="kv-value mono">{context.model ?? 'not reported'}</span>
              </li>
              {context.commands.map((command) => (
                <li key={command.kind}>
                  <span className="kv-key">
                    <span className={`pill ${command.confirmed ? 'ok' : 'warn'}`}>
                      {command.confirmed ? 'confirmed' : 'unconfirmed'}
                    </span>
                    {command.kind}
                  </span>
                  <span className="kv-value mono">{command.command}</span>
                </li>
              ))}
            </ul>
            <ContextManifestView manifest={context.manifest} />
          </>
        ) : (
          <p className="status">No implementation context has been generated yet.</p>
        )}
        <div className="button-row">
          <button
            type="button"
            disabled={Boolean(busy) || generatingContext !== null}
            onClick={openContextModal}
          >
            {generatingContext ? 'Generating context…' : 'Regenerate context'}
          </button>
        </div>
      </CollapsibleCard>

      {delegation && (
        <div className="detail-status-row planning-work-status">
          <span className="pill muted">revision {delegation.delegation.revision}</span>
          <span
            className={`pill ${delegation.delegation.status === 'completed' ? 'ok' : delegation.delegation.status === 'halted' ? 'err' : 'warn'}`}
          >
            {delegation.delegation.status}
          </span>
          <span className="planning-work-progress">
            {delegation.items.filter((entry) => entry.state === 'completed').length} of{' '}
            {delegation.items.length} completed
          </span>
        </div>
      )}

      {!delegation && context && (
        <section className="card">
          <div className="card-header">
            <h2>Work-item decomposition</h2>
            <span className="pill ok">Context ready</span>
          </div>
          <div className="card-body">
            <p>
              Confirmed commands:{' '}
              {context.commands
                .filter((command) => command.confirmed)
                .map((command) => command.command)
                .join(', ') || 'none'}
            </p>
            <button
              type="button"
              className="primary"
              disabled={Boolean(busy) || watching?.kind === 'delegation'}
              onClick={() =>
                void watchTurn(
                  'delegation',
                  'delegation',
                  'Work-item decomposition',
                  () => generateDelegation(projectName, sessionId),
                )
              }
            >
              {watching?.kind === 'delegation'
                ? 'Generating work items…'
                : 'Generate work items'}
            </button>

            {watching?.kind === 'delegation' && (
              <TurnConsole
                projectName={projectName}
                sessionId={sessionId}
                kind="delegation"
                jobId={watching.jobId}
                title={watching.title}
                onFinished={() => {
                  clearWatch()
                  reload()
                }}
              />
            )}
          </div>
        </section>
      )}

      {delegation && driveReady(delegation) && (
        // The graph already knows the order and what is blocked, so this
        // is the same work as clicking every Run button in turn — without
        // needing somebody present between the items.
        <section className="card">
          <div className="card-header">
            <h2>Run unattended</h2>
            <span className="pill muted">
              {delegation.ready.length} ready
            </span>
          </div>
          <div className="card-body">
            <p>
              Runs every ready work item in wave order and merges each one
              that verifies. A failed item stops its dependents only —
              unrelated items keep going, and the delegation halts at the
              end with what failed.
            </p>
            <button
              type="button"
              className="primary"
              disabled={Boolean(busy) || watching !== null}
              onClick={() =>
                void watchTurn(
                  'drive',
                  'drive',
                  `Unattended run: ${delegation.items.length} work items`,
                  () =>
                    driveDelegation(
                      projectName,
                      sessionId,
                      delegation.delegation.id,
                    ),
                )
              }
            >
              {watching?.kind === 'drive'
                ? 'Running work items…'
                : `Run all ${delegation.items.length} work items`}
            </button>

            {watching?.kind === 'drive' && (
              <TurnConsole
                projectName={projectName}
                sessionId={sessionId}
                kind="drive"
                jobId={watching.jobId}
                title={watching.title}
                onFinished={() => {
                  clearWatch()
                  reload()
                }}
              />
            )}
          </div>
        </section>
      )}

      {delegation?.delegation.status === 'halted' ? (
        // A halt disables every Run item button, so the resume control sits
        // next to the buttons it re-enables.
        <section className="card card-alert" role="alert">
          <div className="card-header">
            <h2>Delegation halted</h2>
            <span className="pill err">halted</span>
          </div>
          <div className="card-body">
            <p>
              Work items cannot run while the delegation is halted. It stopped
              for this reason:
            </p>
            <p className="status status-error">
              {delegation.delegation.error || 'No reason was recorded'}
            </p>
            <button
              type="button"
              className="primary"
              disabled={Boolean(busy)}
              onClick={() =>
                void runAction('resume', () =>
                  resumeDelegation(projectName, sessionId, delegation.delegation.id),
                )
              }
            >
              {busy === 'resume' ? 'Resuming…' : 'Resume and re-enable work items'}
            </button>
          </div>
        </section>
      ) : (
        delegation?.delegation.error && (
          <p className="status status-error" role="alert">
            {delegation.delegation.error}
          </p>
        )
      )}

      {delegation &&
        delegation.waves.map((wave, index) => (
          <section className="delegation-wave" key={`wave-${index + 1}`}>
            <div className="delegation-wave-heading">
              <h2>Wave {index + 1}</h2>
              <span className="pill muted">
                {wave.length} item{wave.length === 1 ? '' : 's'}
              </span>
            </div>
            {wave.map((key) => {
              const entry = delegation.items.find((item) => item.item.key === key)
              if (!entry) return null
              return (
                <ItemCard
                  key={key}
                  entry={entry}
                  delegation={delegation}
                  busy={busy}
                  watching={watching?.kind === 'run' ? watching.jobId : null}
                  projectName={projectName}
                  sessionId={sessionId}
                  onReload={reload}
                  onRun={() =>
                    void watchTurn(`run-${key}`, 'run', entry.item.title, () =>
                      startWorkItem(projectName, sessionId, delegation.delegation.id, key),
                    )
                  }
                  onSetRouting={(provider, model) =>
                    void runAction(`route-${key}`, () =>
                      setItemRouting(
                        projectName,
                        sessionId,
                        delegation.delegation.id,
                        key,
                        provider,
                        model,
                      ),
                    )
                  }
                  onClearRouting={() =>
                    void runAction(`route-${key}`, () =>
                      clearItemRouting(
                        projectName,
                        sessionId,
                        delegation.delegation.id,
                        key,
                      ),
                    )
                  }
                />
              )
            })}
          </section>
        ))}
    </>
  )
}
