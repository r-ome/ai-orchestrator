const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** Carries the HTTP status so callers can tell "absent" (404) from "broken". */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function formatErrorDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail)) {
    if (!detail || typeof detail !== 'object') return null

    const message =
      'message' in detail && typeof detail.message === 'string'
        ? detail.message
        : null
    if (!message) return null

    if ('blocking_writer' in detail) {
      const blockingWriter = detail.blocking_writer
      if (
        !blockingWriter ||
        typeof blockingWriter !== 'object' ||
        !('class' in blockingWriter) ||
        typeof blockingWriter.class !== 'string' ||
        !('id' in blockingWriter) ||
        typeof blockingWriter.id !== 'string'
      ) {
        return message
      }
      return `${message} (blocked by ${blockingWriter.class} ${blockingWriter.id})`
    }

    if ('blocking_lease' in detail) {
      const blockingLease = detail.blocking_lease
      if (
        !blockingLease ||
        typeof blockingLease !== 'object' ||
        !('operation' in blockingLease) ||
        typeof blockingLease.operation !== 'string' ||
        !('operation_id' in blockingLease) ||
        typeof blockingLease.operation_id !== 'string'
      ) {
        return message
      }
      return `${message} (${blockingLease.operation} in progress, operation ${blockingLease.operation_id})`
    }

    return message
  }

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
    throw new ApiError(detail, response.status)
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
