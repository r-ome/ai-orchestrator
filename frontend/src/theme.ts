// Theme choice is a user preference, not server state, so it lives in
// localStorage and is stamped onto <html data-theme> for the CSS in
// index.css to key off. The inline script in index.html applies the stored
// choice before first paint; this module is the same logic for React.

export type ThemeChoice = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

// Keep this string in sync with the inline script in index.html.
export const THEME_STORAGE_KEY = 'orchestrator.theme'

export const DARK_QUERY = '(prefers-color-scheme: dark)'

function isChoice(value: string | null): value is ThemeChoice {
  return value === 'system' || value === 'light' || value === 'dark'
}

export function readStoredTheme(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isChoice(stored) ? stored : 'system'
  } catch {
    // Safari in private mode throws on localStorage access.
    return 'system'
  }
}

export function storeTheme(choice: ThemeChoice): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, choice)
  } catch {
    // A theme that does not survive a reload still beats a crash.
  }
}

export function systemTheme(): ResolvedTheme {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light'
}

export function resolveTheme(choice: ThemeChoice): ResolvedTheme {
  return choice === 'system' ? systemTheme() : choice
}

export function applyTheme(choice: ThemeChoice): ResolvedTheme {
  const resolved = resolveTheme(choice)
  document.documentElement.dataset.theme = resolved
  return resolved
}
