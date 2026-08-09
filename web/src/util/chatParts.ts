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

export function groupChatMessages(messages: ChatMessage[]): ChatGroup[] {
  const groups: ChatGroup[] = []
  for (const msg of messages || []) {
    const role = (msg.role || 'unknown').toLowerCase()
    const parts = normalizeChatParts(msg.parts || [])
    if (parts.length === 0) continue
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
