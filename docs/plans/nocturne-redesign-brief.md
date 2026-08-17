# Nocturne redesign — implementation brief

Shared context for every stage. Read this first, then your stage prompt.

## Where things are

- Mockups: `docs/AI orchestrator redesign(1)/*.dc.html`
- Design system: `docs/AI orchestrator redesign(1)/_ds/nocturne-5d5c5f44-8660-441f-857c-998fcf575867/`
  - `styles.css` — the token sheet (`:root` ramps) plus component classes
  - `readme.md` — the written rules (read it; it is short and normative)
- App under change: `frontend/` (React 19, Vite, react-router 7, plain CSS)
  - Global CSS: `frontend/src/index.css` (160 lines), `frontend/src/App.css` (2495 lines)
  - Theme switch: `frontend/src/theme.ts`, `frontend/src/hooks/useTheme.ts`, `frontend/src/App.tsx`

## The mockups are not code

`.dc.html` files are design-tool exports. They use inline styles, fake data,
and a `<x-dc>` wrapper. Do **not** copy their inline styles into React.
Read them as pictures. Reproduce the layout, spacing, hierarchy, and
component vocabulary using the app's own CSS classes and the Nocturne tokens.

Each mockup may show several competing directions side by side (`id="1a"`,
`id="1b"`, `id="1c"`). Your stage prompt names the one to build. Ignore the
others and ignore the badge chips and "layout direction" tags — those are
mockup furniture, not UI.

## Theming rule (decided, not negotiable)

Nocturne is dark-only. The app supports light / dark / system and that must
keep working. So:

- Port Nocturne's token values into the app's existing CSS custom properties
  in `index.css` / `App.css`. Do **not** link `styles.css` and do **not**
  copy the design-system file into `frontend/`.
- Nocturne's published values become the **dark** theme.
- Derive a matching **light** theme by inverting the ramp steps
  (100 ↔ 900, 200 ↔ 800, …) and keeping the same accent hue. Contrast for
  body text must stay at least 4.5:1 against its background in both themes.
- Every color, radius, spacing, and shadow in changed code comes from a
  variable. No new hard-coded hex values, no raw px where a `--space-*` or
  `--radius-*` token fits.
- The appearance segmented control in the left rail keeps working.

## Style rules carried over from Nocturne's readme

- Primary buttons are a 1px accent outline on transparent, never a solid fill.
- Hierarchy is size and space, not weight. Headings stay at weight 500.
- Focus is `:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px }`.
  Never leave the default browser ring.
- Dense on purpose: the spacing scale is 0.7×.
- Do not flood large areas with the accent. It is a line and a glow.
- Monospace runs (ids, paths, models, costs) use the app's existing mono
  stack — do not add a Google Fonts import for Fira Code. Same for Inter:
  if the app does not already load it, use the existing font stack rather
  than adding a network font.

## Non-negotiable constraints

1. **No behaviour changes.** This is a visual and layout redesign. Do not
   change API calls, data shapes, routing paths, polling, or business logic.
   If the mockup implies data the API does not return, render what the API
   has and leave a `// TODO(redesign):` comment naming the missing field.
   Never invent placeholder data in the running app.
2. **No new dependencies.** No icon package, no CSS framework, no
   styled-components. Icons are inline SVG, copied from the mockups.
3. **Accessibility does not regress.** Keep existing roles, `aria-*`,
   labels, and keyboard behaviour. Interactive things stay real `<button>`
   and `<a>` elements.
4. **TypeScript strict must pass.** Run `cd frontend && npm run build` and
   `npm run lint`. Both must be clean before you finish.
5. Keep comments in the codebase's existing voice: explain *why*, not what.

## Definition of done for every stage

- `npm run build` passes (this runs `tsc -b`).
- `npm run lint` (oxlint) passes with no new warnings.
- No file left with unused imports or dead CSS you orphaned.
- A short summary listing every file touched and anything you could not do.
