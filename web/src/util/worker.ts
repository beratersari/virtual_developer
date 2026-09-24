import type { JobItem } from '../api/types'

export type WorkerId = 'opencode' | 'codex' | 'claude'

export function resolveJobWorker(job: JobItem | null | undefined, fallback = ''): WorkerId {
  const raw = (job?.backend || '').trim().toLowerCase()
  if (raw === 'codex' || raw === 'openai' || raw === 'openai-codex') return 'codex'
  if (raw === 'claude' || raw === 'claude-code' || raw === 'anthropic') return 'claude'
  if (raw === 'opencode' || raw === 'open-code' || raw === 'omo') return 'opencode'
  const desc = job?.description || ''
  if (/\bbackend\s*:\s*(openai-)?codex\b/i.test(desc) || /\bworker\s*:\s*codex\b/i.test(desc)) {
    return 'codex'
  }
  if (/\bbackend\s*:\s*claude(-code)?\b/i.test(desc) || /\bworker\s*:\s*claude(-code)?\b/i.test(desc)) {
    return 'claude'
  }
  if (/\bbackend\s*:\s*opencode\b/i.test(desc)) return 'opencode'
  const sid = (job?.opencode_session_id || '').trim()
  if (sid.startsWith('ses_')) return 'opencode'
  // Codex thread ids and Claude session ids are both UUIDs. A bare UUID
  // stays Codex unless Backend was stored or written in {params}.
  if (sid.includes('-') && sid.length >= 16) return 'codex'
  const fb = fallback.trim().toLowerCase()
  if (fb === 'codex') return 'codex'
  if (fb === 'claude' || fb === 'claude-code') return 'claude'
  return 'opencode'
}

export function workerLabel(id: WorkerId): string {
  if (id === 'codex') return 'Codex'
  if (id === 'claude') return 'Claude Code'
  return 'OpenCode'
}

export function sessionKindLabel(id: WorkerId): string {
  if (id === 'codex') return 'thread'
  if (id === 'claude') return 'claude session'
  return 'session'
}

export function sessionIdLabel(id: WorkerId): string {
  if (id === 'codex') return 'Codex thread'
  if (id === 'claude') return 'Claude session'
  return 'OpenCode session'
}
