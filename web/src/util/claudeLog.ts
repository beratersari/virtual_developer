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

const LEAKED_JSON = /"error"|"type"|"usage"|tool_use_id|deprecation_notice|total_cost_usd/

function leakedJsonText(obj: Record<string, unknown>): string {
  const err = obj.error
  if (typeof err === 'string' && err.trim()) return err.trim()
  if (err && typeof err === 'object') {
    const message = (err as Record<string, unknown>).message
    if (typeof message === 'string' && message.trim()) return message.trim()
  }
  if (typeof obj.message === 'string' && obj.message.trim() && !obj.message.includes('{')) {
    return obj.message.trim()
  }
  return ''
}

/** Drop a Claude/proxy JSON blob glued onto an error sentence, even if the brace was cut off. */
function stripLeakedJson(text: string): string {
  if (!text.includes('{')) return text
  let kept = ''
  let cursor = 0
  while (cursor < text.length) {
    const brace = text.indexOf('{', cursor)
    if (brace < 0) {
      kept += text.slice(cursor)
      break
    }
    const chunk = text.slice(brace)
    if (!LEAKED_JSON.test(chunk)) {
      kept += text.slice(cursor, brace + 1)
      cursor = brace + 1
      continue
    }
    const end = jsonObjectEnd(chunk, 0)
    kept += text.slice(cursor, brace)
    if (end <= 0) break
    try {
      kept += leakedJsonText(JSON.parse(chunk.slice(0, end)) as Record<string, unknown>)
    } catch {
      /* closed blob that is not one object */
    }
    cursor = brace + end
  }
  return kept.replace(/\s+·\s*$/g, '').replace(/[ \t]{2,}/g, ' ').trim()
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
  const prose = collapse(stripLeakedJson(plain.join('\n')))
  const extracted = stripLeakedJson(takenText(state))
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

function toolSummary(input: unknown): string {
  if (!input || typeof input !== 'object') return ''
  const rec = input as Record<string, unknown>
  const preferred = ['command', 'file_path', 'filePath', 'path', 'pattern', 'query']
  const bits: string[] = []
  for (const key of preferred) {
    const value = rec[key]
    if (typeof value === 'string' && value.trim()) bits.push(value.trim())
  }
  if (!bits.length) {
    try {
      bits.push(JSON.stringify(input))
    } catch {
      return ''
    }
  }
  const text = bits.join(' · ')
  return text.length > 500 ? `${text.slice(0, 500)}…` : text
}

function unescapeJsonString(value: string): string {
  try {
    return JSON.parse(`"${value}"`) as string
  } catch {
    return value.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"')
  }
}

/** Pull a tool result out of a stream line that was cut off before the closing brace. */
function salvageTruncated(piece: string): CodexLogEvent | null {
  const marker = '"type":"tool_result","content":"'
  const alt = '"type": "tool_result", "content": "'
  let at = piece.indexOf(marker)
  let width = marker.length
  if (at < 0) {
    at = piece.indexOf(alt)
    width = alt.length
  }
  if (at < 0) return null
  const raw = piece.slice(at + width)
  const body = unescapeJsonString(raw).trim()
  if (!body) return null
  const preview = body.length > 800 ? `${body.slice(0, 800)}…` : body
  return { kind: 'command', title: 'tool result', body: preview }
}

function withoutRawEnvelope(text: string): string {
  const trimmed = text.trim()
  if (!trimmed.includes('{')) return trimmed
  return trimmed
    .replace(/\{[^{}]*\}/g, (chunk) => {
      try {
        const obj = JSON.parse(chunk) as Record<string, unknown>
        const inner = asText(obj.message || obj.error || obj.content || obj.result).trim()
        return inner || ''
      } catch {
        return ''
      }
    })
    .replace(/\s+/g, ' ')
    .trim()
}

function foldTranscriptText(value: string): string {
  let text = value.replace(/\s+/g, ' ').trim()
  while (text.includes('\\\\')) text = text.replace(/\\\\/g, '\\')
  return text
}

function sameTranscriptText(a: string, b: string): boolean {
  const left = foldTranscriptText(a)
  const right = foldTranscriptText(b)
  return Boolean(left) && left === right
}

function retryScalar(value: unknown): string {
  if (typeof value === 'string') return stripLeakedJson(value).trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return ''
}

function retryReason(obj: Record<string, unknown>): string {
  const error = obj.error
  if (typeof error === 'string') return stripLeakedJson(error).trim()
  if (error && typeof error === 'object') {
    const rec = error as Record<string, unknown>
    const message = retryScalar(rec.message)
    const type = retryScalar(rec.type)
    if (message && type) return `${type}: ${message}`
    return message || type
  }
  return retryScalar(obj.message)
}

function formatRetryDelay(value: unknown): string {
  const ms = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(ms) || ms < 0) return ''
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms)}ms`
}

/** Claude's api_retry line also carries the HTTP status, the wait, and the request id. */
function formatApiRetry(obj: Record<string, unknown>): string {
  const attempt = obj.attempt != null ? String(obj.attempt) : ''
  const max = obj.max_retries ?? obj.maxRetries
  const count = attempt && max != null && String(max) !== '' ? `${attempt} of ${max}` : attempt
  const why = retryReason(obj) || 'retry'
  const lines = [`API retry${count ? ` ${count}` : ''}: ${why}`]
  const status = obj.error_status ?? obj.status_code
  const statusText = retryScalar(status)
  if (statusText) lines.push(`HTTP ${statusText}`)
  const delay = formatRetryDelay(obj.retry_delay_ms ?? obj.retryDelayMs)
  if (delay) lines.push(`next try in ${delay}`)
  const requestId = retryScalar(obj.request_id ?? obj.requestId)
  if (requestId) lines.push(`request ${requestId}`)
  const extra = retryScalar(obj.error_message ?? obj.message)
  if (extra && extra !== why && !why.includes(extra)) lines.push(extra)
  return lines.join('\n')
}

/** One dashboard row per assistant text, tool call, result, or plain log line. */
export function claudeTranscriptEventsFromLog(raw: string): CodexLogEvent[] {
  const events: CodexLogEvent[] = []
  const text = normalize(raw).replace(ANSI, '')
  const plain: string[] = []
  const flushPlain = () => {
    const body = stripLeakedJson(plain.join('\n').trim())
    plain.length = 0
    if (!body) return
    if (events.some((ev) => sameTranscriptText(ev.body || '', body))) return
    events.push({ kind: 'message', title: 'Claude Code', body })
  }
  for (const line of text.split('\n')) {
    const piece = line.trim()
    if (!piece) continue
    if (!piece.startsWith('{')) {
      const shown = stripLeakedJson(stripCliDiagnostics(piece).trim())
      if (shown) plain.push(shown)
      continue
    }
    flushPlain()
    let parsed: unknown
    try {
      parsed = JSON.parse(piece)
    } catch {
      const saved = salvageTruncated(piece)
      if (saved) events.push(saved)
      continue
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) continue
    const obj = parsed as Record<string, unknown>
    if (isDiagnosticObject(obj)) continue
    const kind = String(obj.type || '')
    if (kind === 'system') {
      const sub = String(obj.subtype || '')
      if (sub === 'api_retry') {
        events.push({ kind: 'meta', title: 'Claude Code', body: formatApiRetry(obj) })
      }
      continue
    }
    if (kind === 'user') {
      const message = (obj.message && typeof obj.message === 'object' ? obj.message : {}) as Record<string, unknown>
      const content = message.content
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== 'object') continue
          const rec = block as Record<string, unknown>
          if (rec.type === 'text') {
            const said = stripLeakedJson(asText(rec.text).trim())
            if (said) events.push({ kind: 'user', title: 'You', body: said })
            continue
          }
          if (rec.type !== 'tool_result') continue
          const body = asText(rec.content).trim()
          if (!body) continue
          if (!body) continue
          const shown = stripLeakedJson(body)
          if (!shown) continue
          const preview = shown.length > 800 ? `${shown.slice(0, 800)}…` : shown
          events.push({
            kind: rec.is_error ? 'error' : 'command',
            title: 'tool result',
            body: preview,
          })
        }
      }
      continue
    }
    if (kind === 'assistant') {
      const message = (obj.message && typeof obj.message === 'object' ? obj.message : {}) as Record<string, unknown>
      const content = message.content
      const said = stripLeakedJson(textFromContent(content))
      if (said) events.push({ kind: 'message', title: 'Claude Code', body: said })
      if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== 'object') continue
          const rec = block as Record<string, unknown>
          if (rec.type !== 'tool_use') continue
          const name = String(rec.name || 'tool')
          events.push({ kind: 'command', title: name, body: toolSummary(rec.input) })
        }
      }
      continue
    }
    if (kind === 'result' || obj.result != null || obj.is_error != null) {
      const result = stripLeakedJson(withoutRawEnvelope(asText(obj.result).trim() || asText(obj.error).trim()))
      if (result && !events.some((ev) => sameTranscriptText(ev.body || '', result))) {
        events.push({
          kind: obj.is_error ? 'error' : 'message',
          title: 'Claude Code',
          body: result,
        })
      }
      continue
    }
  }
  flushPlain()
  return events
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
    const streamed = claudeTranscriptEventsFromLog(log.content || '')
    const hasTurns = streamed.some((ev) => ev.kind === 'message' || ev.kind === 'command' || ev.kind === 'error')
    if (hasTurns) {
      events.push(...streamed)
      continue
    }
    const body = claudeDisplayText(log.content || '')
    if (body) events.push({ kind: 'message', title: 'Claude Code', body })
  }
  return events
}
