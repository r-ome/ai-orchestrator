import { useRef, type KeyboardEvent } from 'react'

export interface TabDefinition<Id extends string> {
  id: Id
  label: string
  /** Optional count or state shown as a pill beside the label. */
  badge?: string
  disabled?: boolean
}

interface TabsProps<Id extends string> {
  label: string
  tabs: TabDefinition<Id>[]
  active: Id
  onSelect: (id: Id) => void
}

/**
 * A tablist following the WAI-ARIA manual-activation pattern: arrows move
 * focus between tabs and Enter or Space selects. Panels are the caller's, and
 * each needs `role="tabpanel"` plus `id={`panel-${id}`}` to match the
 * aria-controls set here.
 */
function Tabs<Id extends string>({ label, tabs, active, onSelect }: TabsProps<Id>) {
  const listRef = useRef<HTMLDivElement>(null)

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
    if (step === 0) return

    const enabled = tabs.filter((tab) => !tab.disabled)
    const current = enabled.findIndex((tab) => tab.id === active)
    if (current < 0) return

    event.preventDefault()
    const next = enabled[(current + step + enabled.length) % enabled.length]
    onSelect(next.id)
    listRef.current
      ?.querySelector<HTMLButtonElement>(`#tab-${next.id}`)
      ?.focus()
  }

  return (
    <div className="tablist" role="tablist" aria-label={label} ref={listRef} onKeyDown={onKeyDown}>
      {tabs.map((tab) => {
        const selected = tab.id === active
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`tab-${tab.id}`}
            className={`tab${selected ? ' tab-active' : ''}`}
            aria-selected={selected}
            aria-controls={`panel-${tab.id}`}
            // Only the selected tab is in the tab order, so Tab leaves the
            // tablist rather than walking every tab in it.
            tabIndex={selected ? 0 : -1}
            disabled={tab.disabled}
            onClick={() => onSelect(tab.id)}
          >
            {tab.label}
            {tab.badge && <span className="pill muted tab-badge">{tab.badge}</span>}
          </button>
        )
      })}
    </div>
  )
}

export default Tabs
