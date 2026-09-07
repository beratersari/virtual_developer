/** Parse ``codex exec --json`` JSONL into display rows.

``codex exec --json`` does not emit the operator prompt. Pair each run log
with its sibling ``*.prompt.txt`` (or any JSONL user turn if present).
*/

import { findPromptForJobPath } from './paths'

export type CodexLogEvent = {
  kind: 'user' | 'message' | 'command' | 'error' | 'meta'
  title: string
  body?: string
}

function contentPartsText(value: unknown): string {
  if (typeof value === 'string') return value
  if (!Array.isArray(value)) return ''
  const bits: string[] = []
  for (const part of value) {
    if (typeof part === 'string') {
      bits.push(part)
      continue
    }
    if (!part || typeof part !== 'object') continue
    const rec = part as Record<string, unknown>
    const text = rec.text ?? rec.content
    if (typeof text === 'string' && text.trim()) bits.push(text)
  }
  return bits.join('\n')
}

function itemText(item: Record<string, unknown>, obj: Record<string, unknown>): string {
  return (
    contentPartsText(item.text) ||
    contentPartsText(item.content) ||
    contentPartsText(obj.text) ||
    contentPartsText(obj.content)
  ).trim()
}

export function parseCodexExecLog(
  raw: string,
  opts: { userPrompt?: string } = {},
): CodexLogEvent[] {
  const events: CodexLogEvent[] = []
  const prompt = (opts.userPrompt || '').trim()
  if (prompt) {
    events.push({ kind: 'user', title: 'You', body: prompt })
  }
  for (const line of (raw || '').split('\n')) {
    const text = line.trim()
    if (!text.startsWith('{')) continue
    let obj: Record<string, unknown>
    try {
      obj = JSON.parse(text) as Record<string, unknown>
    } catch {
      continue
    }
    const type = String(obj.type || '')
    const item =
      obj.item && typeof obj.item === 'object' ? (obj.item as Record<string, unknown>) : {}
    const itemType = String(item.type || '')
    const role = String(item.role || obj.role || '').toLowerCase()
    if (
      itemType === 'user_message' ||
      itemType === 'user' ||
      itemType === 'UserMessage' ||
      ((itemType === 'message' || type === 'message') && role === 'user')
    ) {
      const body = itemText(item, obj)
      if (body && body !== prompt) events.push({ kind: 'user', title: 'You', body })
      continue
    }
    if (itemType === 'agent_message' || itemType === 'message' || role === 'assistant') {
      const body = itemText(item, obj)
      if (body) events.push({ kind: 'message', title: 'Assistant', body })
      continue
    }
    if (itemType === 'command_execution' || itemType === 'command') {
      events.push({
        kind: 'command',
        title: String(item.command || item.cmd || 'command'),
        body: String(item.aggregated_output || item.output || ''),
      })
      continue
    }
    if (type === 'error' || itemType === 'error') {
      const body = String(obj.message || item.message || '').trim()
      if (body) events.push({ kind: 'error', title: 'Error', body })
      continue
    }
    if (type === 'thread.started') {
      const tid = String(obj.thread_id || '')
      if (tid) events.push({ kind: 'meta', title: 'Thread', body: tid })
    }
  }
  return events
}

export function buildCodexTranscriptEvents(
  logs: Array<{ path: string; content?: string }>,
  prompts: Array<{ path: string; content?: string }>,
): CodexLogEvent[] {
  const out: CodexLogEvent[] = []
  const used = new Set<string>()
  for (const log of logs || []) {
    const match = findPromptForJobPath(prompts, null, log.path)
    const prompt = (match?.content || '').trim()
    if (match) used.add(match.path)
    out.push(...parseCodexExecLog(log.content || '', { userPrompt: prompt }))
  }
  if (!out.some((ev) => ev.kind === 'user')) {
    const leftover = (prompts || []).find((p) => !used.has(p.path) && (p.content || '').trim())
    if (leftover?.content?.trim()) {
      out.unshift({ kind: 'user', title: 'You', body: leftover.content.trim() })
    }
  }
  return out
}
