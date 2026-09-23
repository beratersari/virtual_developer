/** Turn a Claude Code run log into the reply the operator should read.

Claude's ``--output-format json`` stdout is one object (sometimes
pretty-printed). ``stream-json`` is one JSON object per line. The CLI also
prints ``[claude-code:…] {…}`` diagnostics on stderr, and older session files
store that warning after the reply. None of those are the assistant message.
*/

import type { TextArtifact } from '../api/types'
import type { CodexLogEvent } from './codexLog'

const CLI_TAG = /\[claude-code:[^\]]+\]/g
const ANSI = /\u001b\[[0-9;]*m/g

function normalize(raw: string): string {
  return (raw || '').replace(/^\uFEFF/, '').replace(/\r\n/g, '\n')
}

function collapse(text: string): string {
  const lines = text.split('\n').map((line) => line.replace(/[ \t]{2,}/g, ' ').replace(/[ \t]+$/g, ''))
  return lines.join('\n').replace(/\n{3,}/g, '\n\n').trim()
}

/** Index just past the `{…}` that starts at `start`, respecting strings. */
function jsonObjectEnd(text: string, start: number): number {
  if (text[start] !== '{') return -1
  let depth = 0
  let inStr = false
  let escape = false
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i]
    if (inStr) {
      if (escape) {
        escape = false
        continue
      }
      if (ch === '\\') {
        escape = true
        continue
      }
      if (ch === '"') inStr = false
      continue
    }
    if (ch === '"') {
      inStr = true
      continue
    }
    if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) return i + 1
    }
  }
  return -1
}

function stripCliDiagnostics(raw: string): string {
  const text = normalize(raw).replace(ANSI, '')
  let out = ''
  let last = 0
  for (const match of text.matchAll(CLI_TAG)) {
    const at = match.index ?? 0
    out += text.slice(last, at)
    let i = at + match[0].length
    while (i < text.length && (text[i] === ' ' || text[i] === '\t')) i += 1
    if (text[i] === '{') {
      const end = jsonObjectEnd(text, i)
      if (end > 0) i = end
    }
    last = i
  }
  out += text.slice(last)
  return out
}

function textFromContent(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts: string[] = []
  for (const block of content) {
    if (typeof block === 'string') {
      if (block.trim()) parts.push(block)
      continue
    }
    if (!block || typeof block !== 'object') continue
    const rec = block as Record<string, unknown>
    if (rec.type === 'text' && typeof rec.text === 'string' && rec.text.trim()) {
      parts.push(rec.text)
    }
  }
  return parts.join('\n').trim()
}

function asText(value: unknown): string {
  if (typeof value === 'string') return value
  return textFromContent(value)
}

function isDiagnosticObject(obj: Record<string, unknown>): boolean {
  return (
    obj.model != null &&
    obj.query_source != null &&
    obj.result == null &&
    obj.message == null &&
    obj.type == null
  )
}

function isClaudeEvent(obj: Record<string, unknown>): boolean {
  const kind = String(obj.type || '')
  if (kind === 'result' || kind === 'assistant' || kind === 'user' || kind === 'system') {
    return true
  }
  if (kind.startsWith('rate_limit')) return true
  if (obj.session_id != null && (obj.result != null || obj.message != null)) return true
  return false
}

type Taken = {
  result: string
  assistant: string[]
  error: string
  isError: boolean
  saw: boolean
}

function take(state: Taken, event: Record<string, unknown>): void {
  if (isDiagnosticObject(event)) {
    state.saw = true
    return
  }
  if (!isClaudeEvent(event)) return
  state.saw = true
  const kind = String(event.type || '')
  if (kind === 'system' || kind.startsWith('rate_limit') || kind === 'user') return
  if (kind === 'assistant' || event.message) {
    const message = event.message
    if (message && typeof message === 'object') {
      const text = textFromContent((message as Record<string, unknown>).content)
      if (text) state.assistant.push(text)
    }
    return
  }
  if (kind === 'result' || event.result != null || event.is_error != null) {
    state.isError = Boolean(event.is_error)
    const result = asText(event.result).trim()
    if (result) state.result = result
    const err = asText(event.error).trim()
    if (err) state.error = err
  }
}

function takenText(state: Taken): string {
  if (state.result.trim()) return state.result.trim()
  const parts: string[] = []
  for (const chunk of state.assistant) {
    const bit = chunk.trim()
    if (bit && parts[parts.length - 1] !== bit) parts.push(bit)
  }
  if (parts.length) return parts.join('\n')
  if (state.isError && state.error.trim()) return state.error.trim()
  return ''
}

function emptyTaken(): Taken {
  return { result: '', assistant: [], error: '', isError: false, saw: false }
}

function tryDocument(text: string): string | null {
  const trimmed = text.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    return null
  }
  const state = emptyTaken()
  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const obj = parsed as Record<string, unknown>
    if (!isClaudeEvent(obj) && !isDiagnosticObject(obj)) return null
    take(state, obj)
    return takenText(state)
  }
  if (!Array.isArray(parsed) || parsed.length === 0) return null
  const events = parsed.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Record<
    string,
    unknown
  >[]
  if (!events.some((item) => isClaudeEvent(item) || isDiagnosticObject(item))) return null
  for (const item of events) take(state, item)
  return takenText(state)
}

function walkLines(text: string): string {
  const state = emptyTaken()
  const plain: string[] = []
  for (const line of text.split('\n')) {
    const piece = line.trim()
    if (!piece) {
      plain.push('')
      continue
    }
    if (!piece.startsWith('{') && !piece.startsWith('[')) {
      plain.push(line)
      continue
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(piece)
    } catch {
      plain.push(line)
      continue
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      plain.push(line)
      continue
    }
    const obj = parsed as Record<string, unknown>
    if (isDiagnosticObject(obj)) continue
    if (isClaudeEvent(obj)) {
      take(state, obj)
      continue
    }
    plain.push(line)
  }
  const prose = collapse(plain.join('\n'))
  const extracted = takenText(state)
  if (!prose) return extracted
  if (!extracted || prose.includes(extracted) || extracted.includes(prose)) return prose
  return collapse(`${prose}\n${extracted}`)
}

/** Assistant reply. CLI envelopes and `[claude-code:…]` warnings are removed. */
export function claudeDisplayText(raw: string): string {
  const text = normalize(raw)
  const direct = tryDocument(text)
  if (direct != null) return collapse(stripCliDiagnostics(direct))
  const noAnsi = text.replace(ANSI, '')
  if (noAnsi !== text) {
    const retry = tryDocument(noAnsi)
    if (retry != null) return collapse(stripCliDiagnostics(retry))
  }
  const cleaned = stripCliDiagnostics(noAnsi)
  const after = tryDocument(cleaned)
  if (after != null) return collapse(stripCliDiagnostics(after))
  return collapse(stripCliDiagnostics(walkLines(cleaned)))
}

/** Historical prompts still say "OpenCode agent:" — Claude jobs are not OpenCode. */
export function claudePromptText(raw: string): string {
  return (raw || '').replace(/(^|\n)([ \t]*)OpenCode agent:/g, '$1$2Agent:')
}

export function buildClaudeTranscriptEvents(
  logs: TextArtifact[],
  prompts: TextArtifact[],
): CodexLogEvent[] {
  const events: CodexLogEvent[] = []
  for (const prompt of prompts) {
    const body = claudePromptText(prompt.content || '').trim()
    if (body) events.push({ kind: 'user', title: 'You', body })
  }
  for (const log of logs) {
    const body = claudeDisplayText(log.content || '')
    if (body) events.push({ kind: 'message', title: 'Claude Code', body })
  }
  return events
}
