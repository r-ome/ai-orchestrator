import type { ReactNode } from 'react'

/**
 * Renders the Markdown subset the planning models actually emit: headings,
 * bold, italic, inline code, fenced code, ordered and unordered lists,
 * pipe tables, horizontal rules, and paragraphs.
 *
 * It builds React elements rather than HTML, so model-authored text cannot
 * inject markup: there is no dangerouslySetInnerHTML anywhere below. Anything
 * outside the subset — links, blockquotes — falls through and renders
 * as its own literal text, so nothing a model writes can silently vanish.
 */

const FENCE = /^\s*```/
const FENCE_END = /^\s*```\s*$/
const HEADING = /^(#{1,6})\s+(.+)$/
const RULE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/
const BULLET = /^(\s*)[-*+]\s+(.+)$/
const ORDERED = /^(\s*)\d+[.)]\s+(.+)$/
const BLANK = /^\s*$/
const TABLE_ROW = /^\s*\|/
// A separator cell is dashes with optional alignment colons: `---`, `:--`, `--:`.
const TABLE_ALIGN = /^\s*(:?)-+(:?)\s*$/

// Ordered so `**` is tried before `*`; otherwise bold opens as an emphasis run
// and swallows the closing pair.
const INLINE = /(`[^`]+`)|(\*\*[\s\S]+?\*\*)|(\*[^*\n]+\*)|(_[^_\n]+_)/g

function renderInline(text: string, key: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const pattern = new RegExp(INLINE.source, 'g')
  let cursor = 0
  let token = 0
  let match = pattern.exec(text)

  while (match !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index))
    const raw = match[0]
    const nodeKey = `${key}-i${token++}`

    if (raw.startsWith('`')) {
      // Code first, so `**` inside a span stays literal.
      nodes.push(<code key={nodeKey}>{raw.slice(1, -1)}</code>)
    } else if (raw.startsWith('**')) {
      nodes.push(
        <strong key={nodeKey}>{renderInline(raw.slice(2, -2), nodeKey)}</strong>,
      )
    } else {
      nodes.push(<em key={nodeKey}>{renderInline(raw.slice(1, -1), nodeKey)}</em>)
    }

    cursor = pattern.lastIndex
    match = pattern.exec(text)
  }

  if (cursor < text.length) nodes.push(text.slice(cursor))
  return nodes
}

/**
 * Splits one table row into cells. Pipes inside a code span and pipes escaped
 * as `\|` stay in the cell, which is how a model writes a shell pipeline or a
 * union type in a table.
 */
function splitRow(line: string): string[] {
  const trimmed = line.trim()
  // Drop the delimiting pipes so `| a | b |` is two cells, not four.
  const body = trimmed.endsWith('|') && trimmed.length > 1
    ? trimmed.slice(1, -1)
    : trimmed.slice(1)

  const cells: string[] = []
  let cell = ''
  let inCode = false

  for (let index = 0; index < body.length; index += 1) {
    const char = body[index]
    if (char === '\\' && body[index + 1] === '|') {
      cell += '|'
      index += 1
      continue
    }
    if (char === '`') inCode = !inCode
    if (char === '|' && !inCode) {
      cells.push(cell.trim())
      cell = ''
      continue
    }
    cell += char
  }

  cells.push(cell.trim())
  return cells
}

type Align = 'left' | 'center' | 'right' | undefined

/**
 * Reads the alignment row under a header, or null when the lines are not a
 * table. A pipe line on its own is just text, so the separator is what makes
 * the block a table.
 */
function readAlignments(lines: string[], start: number): Align[] | null {
  if (!TABLE_ROW.test(lines[start])) return null
  if (start + 1 >= lines.length || !TABLE_ROW.test(lines[start + 1])) return null

  const cells = splitRow(lines[start + 1])
  if (cells.length === 0) return null

  const alignments: Align[] = []
  for (const cell of cells) {
    const match = cell.match(TABLE_ALIGN)
    if (match === null) return null
    if (match[1] === ':' && match[2] === ':') alignments.push('center')
    else if (match[2] === ':') alignments.push('right')
    else if (match[1] === ':') alignments.push('left')
    else alignments.push(undefined)
  }

  if (alignments.length !== splitRow(lines[start]).length) return null
  return alignments
}

function isBlockStart(lines: string[], index: number): boolean {
  const line = lines[index]
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    BULLET.test(line) ||
    ORDERED.test(line) ||
    readAlignments(lines, index) !== null
  )
}

/** Collects one list, returning its items and the line after it. */
function readList(
  lines: string[],
  start: number,
  itemPattern: RegExp,
): { items: string[]; next: number } {
  const items: string[] = []
  let index = start

  while (index < lines.length) {
    const item = lines[index].match(itemPattern)
    if (item !== null) {
      items.push(item[2])
      index += 1
      continue
    }
    // A non-blank, non-block line under an item is that item's continuation,
    // which is how a model wraps a long bullet.
    if (
      items.length > 0 &&
      !BLANK.test(lines[index]) &&
      !isBlockStart(lines, index)
    ) {
      items[items.length - 1] += ` ${lines[index].trim()}`
      index += 1
      continue
    }
    break
  }

  return { items, next: index }
}

function renderBlocks(source: string): ReactNode[] {
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let index = 0
  let key = 0

  while (index < lines.length) {
    const line = lines[index]

    if (BLANK.test(line)) {
      index += 1
      continue
    }

    if (FENCE.test(line)) {
      const body: string[] = []
      index += 1
      while (index < lines.length && !FENCE_END.test(lines[index])) {
        body.push(lines[index])
        index += 1
      }
      // An unterminated fence still renders; the model simply forgot to close it.
      if (index < lines.length) index += 1
      blocks.push(
        <pre key={`b${key++}`} className="markdown-code">
          <code>{body.join('\n')}</code>
        </pre>,
      )
      continue
    }

    const heading = line.match(HEADING)
    if (heading !== null) {
      const level = Math.min(heading[1].length, 6)
      const Tag = `h${level}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
      const blockKey = `b${key++}`
      blocks.push(<Tag key={blockKey}>{renderInline(heading[2], blockKey)}</Tag>)
      index += 1
      continue
    }

    // Before the bullet check: `---` has no space and is a rule, not an item.
    if (RULE.test(line)) {
      blocks.push(<hr key={`b${key++}`} />)
      index += 1
      continue
    }

    const alignments = readAlignments(lines, index)
    if (alignments !== null) {
      const header = splitRow(line)
      const rows: string[][] = []
      // The header and the alignment row are consumed; the rest is the body.
      index += 2
      while (index < lines.length && TABLE_ROW.test(lines[index])) {
        const cells = splitRow(lines[index])
        // A short or long row still renders: pad or drop to the header width so
        // the columns stay aligned.
        while (cells.length < header.length) cells.push('')
        rows.push(cells.slice(0, header.length))
        index += 1
      }

      const blockKey = `b${key++}`
      blocks.push(
        <div key={blockKey} className="markdown-table">
          <table>
            <thead>
              <tr>
                {header.map((cell, column) => (
                  <th
                    key={`${blockKey}-h${column}`}
                    style={{ textAlign: alignments[column] }}
                  >
                    {renderInline(cell, `${blockKey}-h${column}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((cells, row) => (
                <tr key={`${blockKey}-r${row}`}>
                  {cells.map((cell, column) => (
                    <td
                      key={`${blockKey}-r${row}c${column}`}
                      style={{ textAlign: alignments[column] }}
                    >
                      {renderInline(cell, `${blockKey}-r${row}c${column}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    if (BULLET.test(line)) {
      const { items, next } = readList(lines, index, BULLET)
      const blockKey = `b${key++}`
      blocks.push(
        <ul key={blockKey}>
          {items.map((item, position) => (
            <li key={`${blockKey}-${position}`}>
              {renderInline(item, `${blockKey}-${position}`)}
            </li>
          ))}
        </ul>,
      )
      index = next
      continue
    }

    if (ORDERED.test(line)) {
      const { items, next } = readList(lines, index, ORDERED)
      const blockKey = `b${key++}`
      blocks.push(
        <ol key={blockKey}>
          {items.map((item, position) => (
            <li key={`${blockKey}-${position}`}>
              {renderInline(item, `${blockKey}-${position}`)}
            </li>
          ))}
        </ol>,
      )
      index = next
      continue
    }

    const paragraph: string[] = []
    while (
      index < lines.length &&
      !BLANK.test(lines[index]) &&
      !isBlockStart(lines, index)
    ) {
      paragraph.push(lines[index].trim())
      index += 1
    }
    const blockKey = `b${key++}`
    blocks.push(
      <p key={blockKey}>{renderInline(paragraph.join(' '), blockKey)}</p>,
    )
  }

  return blocks
}

function Markdown({ source }: { source: string }) {
  if (!source.trim()) return null
  return <div className="markdown">{renderBlocks(source)}</div>
}

export default Markdown
