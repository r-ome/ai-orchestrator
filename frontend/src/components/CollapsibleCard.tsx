import type { ReactNode } from 'react'

interface CollapsibleCardProps {
  title: ReactNode
  /** Pills or buttons pinned to the right of the summary row. */
  aside?: ReactNode
  defaultOpen?: boolean
  /** `h2` for a page-level card, `h3` for one nested inside another card. */
  headingLevel?: 2 | 3
  children: ReactNode
}

/**
 * A `.card` whose header is its own disclosure control.
 *
 * The summary carries the same padding and hairline as `.card-header`, so a
 * collapsed card is the header row alone and an open one is visually identical
 * to a plain card. The aside swallows its own clicks: a button in the summary
 * would otherwise toggle the card as well as run its action. Pass buttons and
 * pills only — the aside cancels the click's default action, which would also
 * cancel a link's navigation.
 */
function CollapsibleCard({
  title,
  aside,
  defaultOpen = false,
  headingLevel = 2,
  children,
}: CollapsibleCardProps) {
  const Heading = headingLevel === 2 ? 'h2' : 'h3'

  return (
    <details className="card collapsible-card" open={defaultOpen}>
      <summary className="card-header collapsible-summary">
        <span className="collapsible-title">
          <span className="collapsible-caret" aria-hidden="true" />
          <Heading>{title}</Heading>
        </span>
        {aside && (
          <span
            className="collapsible-aside"
            // `stopPropagation` alone does not help: React listens at the root,
            // so the summary has already claimed the toggle by the time this
            // runs. Cancelling the default action does, because the browser
            // applies it only once dispatch finishes.
            onClick={(event) => event.preventDefault()}
          >
            {aside}
          </span>
        )}
      </summary>
      <div className="card-body">{children}</div>
    </details>
  )
}

export default CollapsibleCard
