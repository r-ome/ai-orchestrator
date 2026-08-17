# Nocturne stage 1 report

## Files touched

- `frontend/src/index.css` — added the Nocturne dark token foundation, its
  derived light inversion, app-token aliases, and the required source comment.
- `frontend/src/App.css` — replaced hard-coded terminal colors, the rail-mark
  gradient color, component radii, and the primary-hover white mix with tokens.
- `docs/plans/nocturne-stage1-report.md` — this report.

## Token mapping

| App token | Nocturne role |
| --- | --- |
| `--bg`, `--panel`, `--panel2` | `--color-bg`, `--color-surface`, and an accessible inset neutral surface |
| `--border`, `--line` | neutral-500 in dark / neutral-300 in light, used as the visible divider token |
| `--text`, `--text-h` | `--color-text` and the theme's readable neutral foreground |
| `--muted` | neutral-400 in dark and neutral-200 in light; both clear every surface where the shared token is used as text |
| `--accent` | `--color-accent` in dark and the derived accent-700 light value |
| `--ok`, `--warn`, `--err`, `--info` | low-chroma semantic green, amber, red, and blue at Nocturne's shared 300 dark / 700 light steps |
| `--series-reclaimable` | a low-chroma purple categorical slot, separate from `--info` and every status hue |
| `--code-bg`, `--social-bg` | neutral-800 in dark and the accessible light inset surface in light |
| `--shadow` | `--shadow-md` |
| Existing component radii | `--radius-sm`, `--radius-md`, or `--radius-lg` |

The foundation also adds the full neutral and accent 100–900 ramps,
`--color-surface`, `--color-divider`, all three shadow tokens, the 0.7x
spacing scale, and the three radius tokens. The app keeps its existing
Space Grotesk and mono fallback stacks. This follows the brief's instruction
not to add an Inter webfont import.

## Corrected semantic and categorical tokens

Nocturne remains mono for decorative color. State and chart categories are an
exception because they carry operational information. Each semantic value uses
OKLCH chroma **0.060**, below the accent's **0.125**. Dark foregrounds use the
shared Nocturne 300 lightness step (**L 0.870**). Light foregrounds use the
inverted 700 step (**L 0.480**).

| Token | Role hue | Dark value | Dark: bg / panel | Light value | Light: bg / panel |
| --- | --- | --- | --- | --- | --- |
| `--ok` | green, 145° | `#bcdfbc` | 12.09:1 / 10.43:1 | `#486749` | 5.83:1 / 5.15:1 |
| `--warn` | amber, 80° | `#e9d1a8` | 11.86:1 / 10.24:1 | `#6f5a35` | 6.05:1 / 5.34:1 |
| `--err` | red, 25° | `#fac6c0` | 11.66:1 / 10.06:1 | `#7c504c` | 6.21:1 / 5.48:1 |
| `--info` | blue, 250° | `#b7d8fb` | 11.93:1 / 10.29:1 | `#43607e` | 6.01:1 / 5.31:1 |
| `--series-reclaimable` | purple, 320° | `#e6c8ed` | 11.61:1 / 10.02:1 | `#6d5373` | 6.16:1 / 5.44:1 |

The four state hues have pairwise contrast ratios of **1.01:1–1.07:1**. They
are intentionally separated by hue instead: green/amber 65°, green/red 120°,
green/blue 105°, amber/red 55°, amber/blue 170°, and red/blue 135°. The
closest categorical separation is purple/red at 65°. These separations are
clear at a glance and do not depend on lightness alone.

## Foreground and chrome re-check

The sweep includes every shared text and chrome alias against `--bg`,
`--panel`, and `--panel2`. Status and categorical marks use the page and panel
grounds shown above. Text must meet 4.5:1. Interface chrome must meet 3:1.

| Token and use | Dark: bg / panel / panel2 | Light: bg / panel / panel2 |
| --- | --- | --- |
| `--text` body text | 14.54:1 / 12.55:1 / 8.27:1 | 13.01:1 / 11.49:1 / 9.50:1 |
| `--text-h` headings and code text | 16.18:1 / 13.97:1 / 9.20:1 | 13.01:1 / 11.49:1 / 9.50:1 |
| `--muted` body text | 8.75:1 / 7.55:1 / 4.98:1 | 9.20:1 / 8.13:1 / 6.72:1 |
| `--accent` interface chrome | 5.45:1 / 4.71:1 / 3.10:1 | 6.23:1 / 5.50:1 / 4.55:1 |
| `--border`, `--line` interface chrome | 6.08:1 / 5.25:1 / 3.46:1 | 6.02:1 / 5.32:1 / 4.40:1 |

The sweep found four further failures in the previous mapping. The dark
divider and border were below 3:1, the light inset surface made shared muted
and code text fail, and the 30% accent outline fell below the chrome floor.
The corrected tokens use neutral-500 dark / neutral-300 light for divider and
border, an accessible light inset surface, and a full accent outline.

Accent-mark text measures **5.45:1** in dark and **6.23:1** in light. Code
text on `--code-bg` measures **9.20:1** in dark and **9.50:1** in light. The
dark terminal keeps independent light text at **13.01:1** and independent
error text at **9.37:1** against its stable background.

Status pill fills use a 12% tint. Their lowest text contrast is the light ok
pill at **4.67:1**, so the fill does not reduce any status text below AA.

## Deliberately left for later stages

- Page and component layout remains unchanged.
- Component treatment changes, including outlined primary actions and Nocturne
  interaction patterns, remain for the later redesign stages.
- No design-system stylesheet was linked or copied into `frontend/`.

## Verification

- `cd frontend && npm run build` passed.
- `cd frontend && npm run lint` passed with one existing Fast Refresh warning
  in `src/components/DelegationWorkspace.tsx:717`.
