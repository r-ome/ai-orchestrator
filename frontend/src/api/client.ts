const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

function formatErrorDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail)) return null

  const messages = detail.flatMap((error) => {
    if (!error || typeof error !== 'object') return []

    const location = Array.isArray(error.loc)
      ? error.loc.filter((part: unknown) => part !== 'body').join('.')
      : ''
    const message = typeof error.msg === 'string' ? error.msg : ''
    if (!message) return []
    return [location ? `${location}: ${message}` : message]
  })
  return messages.length > 0 ? messages.join('; ') : null
}

async function request<T>(
  path: string,
  init?: RequestInit & { signal?: AbortSignal },
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`
    try {
      const body = await response.json()
      detail = formatErrorDetail(body?.detail) ?? detail
    } catch {
      // Response body was not JSON; keep the status-based message.
    }
    throw new Error(detail)
  }

  return (await response.json()) as T
}

/** GETs a JSON endpoint and turns a non-2xx into an Error carrying the
 *  backend's `detail` message when there is one. */
export function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal })
}

export function postJson<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    signal,
  })
}

export function putJson<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    signal,
  })
}

export function deleteJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'DELETE', signal })
}

export function deleteJsonBody<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return request<T>(path, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}
