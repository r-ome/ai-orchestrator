/**
 * Rendering the assigned model's stream-json output as readable console lines.
 *
 * The turn container runs `claude -p --output-format stream-json --verbose`,
 * which emits one JSON object per line. Everything here turns that firehose
 * into the few things a reader is looking for: what the model touched, whether
 * a tool failed, and how the turn ended.
 */

/** Single stand-in for a run of the model's reasoning, however it is reported. */
export const THINKING_MARKER = '· thinking'

/**
 * Whether a `system` event is just reasoning progress.
 *
 * Matched on the substring rather than the exact name: the CLI has shipped
 * `thinking_tokens`, and a neighbouring spelling would otherwise reintroduce
 * the wall of noise this exists to prevent.
 */
function isThinkingSubtype(subtype: string): boolean {
  return subtype.toLowerCase().includes('thinking')
}

/**
 * Turns one stream-json line into the lines worth reading, or none to drop it.
 *
 * One line per block rather than one joined string per message, so a run of
 * thinking blocks stays collapsible across events in `appendLines`.
 *
 * Returning the raw line for unparseable input is deliberate: a container that
 * dies before the CLI starts writes a plain shell error to stderr, and that is
 * exactly the case where a reader most needs to see it.
 */
export function summarise(line: string): string[] {
  const text = line.trim()
  if (!text) return []

  let event: Record<string, unknown>
  try {
    event = JSON.parse(text) as Record<string, unknown>
  } catch {
    return [text]
  }

  const type = String(event.type ?? '')
  if (type === 'system') {
    const subtype = String(event.subtype ?? '')
    if (!subtype) return []
    // `thinking_tokens` is a progress ping the CLI emits repeatedly while the
    // model reasons — one per update, dozens per turn. Rendered literally it
    // filled the console with `· system: thinking_tokens` and pushed the tool
    // calls and the result out of view. It means the same thing as a thinking
    // block, so it becomes the same marker and collapses with it.
    if (isThinkingSubtype(subtype)) return [THINKING_MARKER]
    return [`· system: ${subtype}`]
  }
  if (type === 'result') {
    const cost = typeof event.total_cost_usd === 'number' ? event.total_cost_usd : null
    const turns = typeof event.num_turns === 'number' ? event.num_turns : null
    const parts = [`· result: ${String(event.subtype ?? 'done')}`]
    if (turns !== null) parts.push(`${turns} turns`)
    if (cost !== null) parts.push(`$${cost.toFixed(4)}`)
    return [parts.join(' · ')]
  }
  if (type === 'assistant' || type === 'user') {
    const content = (event.message as { content?: unknown })?.content
    if (!Array.isArray(content)) return []
    return content.flatMap((block) => renderBlock(block))
  }
  return []
}

function renderBlock(block: unknown): string[] {
  if (!block || typeof block !== 'object') return []
  const entry = block as Record<string, unknown>
  const type = String(entry.type ?? '')

  if (type === 'text') {
    const text = String(entry.text ?? '').trim()
    return text ? [text] : []
  }
  if (type === 'tool_use') {
    const input = entry.input as Record<string, unknown> | undefined
    // The path or command is the part that says what the model is doing; the
    // rest of a tool input is often a whole file and drowns the console.
    const detail =
      (typeof input?.file_path === 'string' && input.file_path) ||
      (typeof input?.command === 'string' && input.command) ||
      (typeof input?.pattern === 'string' && input.pattern) ||
      ''
    return [`→ ${String(entry.name ?? 'tool')}${detail ? `: ${detail}` : ''}`]
  }
  if (type === 'thinking' || type === 'redacted_thinking') {
    // The reasoning itself is not shown. It arrives in many blocks across many
    // events, and printing each one buries the tool calls and the result — the
    // two things a reader is actually looking for. `appendLines` collapses a
    // run of these into the single marker below.
    return [THINKING_MARKER]
  }
  if (type === 'tool_result') {
    return entry.is_error === true ? ['← tool failed'] : []
  }
  return []
}

/**
 * Appends rendered lines, collapsing consecutive thinking markers into one.
 *
 * A single turn produces long stretches of reasoning, so without this the
 * console is mostly repetitions of one line and the 500-line cap throws away
 * the output that matters.
 */
export function appendLines(current: string[], incoming: string[]): string[] {
  const next = [...current]
  for (const line of incoming) {
    if (line === THINKING_MARKER && next[next.length - 1] === THINKING_MARKER) {
      continue
    }
    next.push(line)
  }
  return next
}
