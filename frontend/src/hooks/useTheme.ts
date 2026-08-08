import { useCallback, useEffect, useState } from 'react'
import {
  DARK_QUERY,
  applyTheme,
  readStoredTheme,
  resolveTheme,
  storeTheme,
  type ResolvedTheme,
  type ThemeChoice,
} from '../theme'

interface UseThemeResult {
  choice: ThemeChoice
  resolved: ResolvedTheme
  setChoice: (next: ThemeChoice) => void
}

export function useTheme(): UseThemeResult {
  const [choice, setChoiceState] = useState<ThemeChoice>(readStoredTheme)
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    resolveTheme(readStoredTheme()),
  )

  useEffect(() => {
    setResolved(applyTheme(choice))
  }, [choice])

  // Only 'system' follows the OS. An explicit choice must survive the user
  // changing their OS appearance while the tab is open.
  useEffect(() => {
    if (choice !== 'system') return
    const query = window.matchMedia(DARK_QUERY)
    const sync = () => setResolved(applyTheme('system'))
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [choice])

  const setChoice = useCallback((next: ThemeChoice) => {
    storeTheme(next)
    setChoiceState(next)
  }, [])

  return { choice, resolved, setChoice }
}
