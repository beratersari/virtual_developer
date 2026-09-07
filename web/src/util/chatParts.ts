import type { ChatMessage, ChatPart } from '../api/types'

const SKIP_PART_TYPES = new Set(['step-start', 'step-finish'])
const THINKING_TYPES = new Set(['reasoning', 'thinking'])
const THINK_TAG = /<think(?:ing)?>([\s\S]*?)<\/think(?:ing)?>/gi

export function extractThinkFromText(text: string): { thinking: string; rest: string } {
  const chunks: string[] = []
  const rest = String(text || '').replace(THINK_TAG, (_m, inner: string) => {
    const t = String(inner || '').trim()
    if (t) chunks.push(t)
    return '\n'
  })
  return { thinking: chunks.join('\n\n'), rest: rest.trim() }
}

export function normalizeChatParts(parts: ChatPart[]): ChatPart[] {
  const expanded: ChatPart[] = []
  for (const part of parts || []) {
    const t = (part.type || '').toLowerCase()
    if (SKIP_PART_TYPES.has(t)) continue
    if (t === 'text') {
      const { thinking, rest } = extractThinkFromText(part.text || '')
      if (thinking) {
        expanded.push({
          ...part,
          id: `${part.id || 't'}:think`,
          type: 'reasoning',
          text: thinking,
        })
      }
      if (rest) expanded.push({ ...part, type: 'text', text: rest })
      continue
    }
    if (THINKING_TYPES.has(t)) {
      if ((part.text || '').trim()) expanded.push({ ...part, type: 'reasoning' })
      continue
    }
    expanded.push(part)
  }

  const merged: ChatPart[] = []
  for (const part of expanded) {
    const prev = merged[merged.length - 1]
    if (part.type === 'reasoning' && prev?.type === 'reasoning') {
      prev.text = `${(prev.text || '').trim()}\n\n${(part.text || '').trim()}`
      if (part.truncated) prev.truncated = true
      continue
    }
    merged.push({ ...part })
  }
  return merged
}

export type ChatGroup = {
  key: string
  role: string
  agent?: string | null
  created_at?: string | null
  session_id?: string
  parts: ChatPart[]
}

const CONTINUE_PROMPT_PREFIX = 'continue the previous opencode session'
const FINISH_TODOS_PREFIX = 'finish remaining todos and complete the original task'
const CONTINUE_AFTER_COMPACT_PREFIX = 'continue after context compaction'
const OPENCODE_AUTO_CONTINUE_PREFIX = 'continue if you have next steps'
const HTML_COMMENT = /<!--[\s\S]*?-->/g
const COMPACT_USER_TEXT =
  /session compacted|compacting session|compaction summary|context compacted|continue after context compaction|auto[- ]?compact/i
const INTERNAL_COMPACT_FOLLOWUP =
  /restore checkpointed session|checkpointed session agent configuration|todo continuation|background task completed|system-reminder|continue if you have next steps|continue after context compaction|conversation was compacted|large media attachments/i

export function stripInternalMarkup(text: string): string {
  return String(text || '')
    .replace(HTML_COMMENT, '')
    .replace(/OMO_INTERNAL\w*/gi, '')
    .trim()
}

export function isOrchestratorContinueText(text: string): boolean {
  const t = stripInternalMarkup(text).toLowerCase()
  return (
    t.startsWith(CONTINUE_PROMPT_PREFIX) ||
    t.startsWith(FINISH_TODOS_PREFIX) ||
    t.startsWith(CONTINUE_AFTER_COMPACT_PREFIX)
  )
}

export function isInternalCompactFollowupText(text: string): boolean {
  const raw = (text || '').trim()
  if (!raw) return false
  const cleaned = stripInternalMarkup(raw)
  if (!cleaned) return true
  const t = cleaned.toLowerCase()
  // OpenCode/oh-my-openagent injects these as role=user. Length is unbounded
  // (failed explore subagents paste the full stack). Never label them "You".
  if (t.startsWith('<system-reminder>') || t.includes('<system-reminder>')) return true
  if (t.includes('[all background tasks finished')) return true
  if (t.includes('action required:') && t.includes('background task')) return true
  if (t.startsWith(OPENCODE_AUTO_CONTINUE_PREFIX)) return true
  if (t.startsWith(FINISH_TODOS_PREFIX)) return true
  if (t.startsWith(CONTINUE_AFTER_COMPACT_PREFIX)) return true
  if (t.startsWith('[restore checkpointed')) return true
  if (t.startsWith('the previous request exceeded')) return true
  return cleaned.length < 400 && INTERNAL_COMPACT_FOLLOWUP.test(cleaned)
}

function isCompactionSummary(summary: unknown): boolean {
  if (summary === true) return true
  if (summary && typeof summary === 'object' && (summary as { compaction?: unknown }).compaction) {
    return true
  }
  return false
}

export function chatDisplayRole(msg: ChatMessage, parts: ChatPart[]): string {
  const raw = (msg.role || 'unknown').toLowerCase()
  const visible = parts.filter((p) => p.type !== 'step-start' && p.type !== 'step-finish')
  const types = visible.map((p) => (p.type || '').toLowerCase())
  const text = visible
    .filter((p) => (p.type || 'text') === 'text')
    .map((p) => p.text || '')
    .join('\n')
  if (isOrchestratorContinueText(text) || isInternalCompactFollowupText(text)) return 'skip'
  if (raw === 'compaction') return 'compaction'
  if (types.some((t) => t === 'compaction' || t === 'compact')) return 'compaction'
  const compactAgent = (msg.agent || '').toLowerCase() === 'compaction'
  if (raw === 'summary' || compactAgent || isCompactionSummary(msg.summary)) {
    return stripInternalMarkup(text) ? 'summary' : 'compaction'
  }
  const cleaned = stripInternalMarkup(text)
  if (raw === 'user' && cleaned && cleaned.length < 400 && COMPACT_USER_TEXT.test(cleaned)) {
    return 'compaction'
  }
  return raw
}

export function groupChatMessages(messages: ChatMessage[]): ChatGroup[] {
  const groups: ChatGroup[] = []
  for (const msg of messages || []) {
    const parts = normalizeChatParts(msg.parts || []).map((p) => {
      if ((p.type || 'text') !== 'text' || !p.text) return p
      return { ...p, text: stripInternalMarkup(p.text) }
    })
    if (parts.length === 0) continue
    const role = chatDisplayRole(msg, parts)
    if (role === 'skip') continue
    const last = groups[groups.length - 1]
    const sameAssistantTurn =
      last &&
      last.role === 'assistant' &&
      role === 'assistant' &&
      (last.session_id || '') === (msg.session_id || '')
    if (sameAssistantTurn) {
      last.parts = normalizeChatParts([...last.parts, ...parts])
      if (msg.created_at) last.created_at = msg.created_at
      if (msg.agent) last.agent = msg.agent
      continue
    }
    groups.push({
      key: msg.id || `${msg.session_id || 'ses'}-${msg.created_at || groups.length}`,
      role,
      agent: msg.agent,
      created_at: msg.created_at,
      session_id: msg.session_id,
      parts,
    })
  }
  return groups
}
