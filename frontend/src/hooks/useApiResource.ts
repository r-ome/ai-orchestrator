import { useCallback, useEffect, useState } from 'react'

interface ApiResource<T> {
  data: T | null
  /** True only until the first result arrives. */
  loading: boolean
  /** True while any fetch is in flight, including background refreshes. */
  refreshing: boolean
  error: string | null
  reload: () => void
}

/**
 * Loads a resource on mount, exposes loading and error state, and aborts the
 * request when the component unmounts or a reload starts.
 *
 * A reload keeps the previous data on screen. Callers gate their UI on
 * `loading`, so blanking it mid-refresh would unmount the subtree and discard
 * any state it holds (an expanded row, a scroll position).
 */
export function useApiResource<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  /** Values that, when changed, should trigger a refetch (e.g. a route param). */
  deps: readonly unknown[] = [],
): ApiResource<T> {
  const [data, setData] = useState<T | null>(null)
  const [refreshing, setRefreshing] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const reload = useCallback(() => setReloadToken((token) => token + 1), [])

  useEffect(() => {
    const controller = new AbortController()

    setRefreshing(true)

    fetcher(controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setData(result)
        setError(null)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : 'Unknown error')
      })
      .finally(() => {
        if (!controller.signal.aborted) setRefreshing(false)
      })

    return () => controller.abort()
    // `fetcher` is recreated each render by callers that close over a param,
    // so the refetch triggers are `reloadToken` plus the caller's own deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadToken, ...deps])

  return { data, loading: refreshing && data === null, refreshing, error, reload }
}
