import type { JobItem } from '../api/types'

export type WorkerId = 'opencode' | 'codex'

export function resolveJobWorker(job: JobItem | null | undefined, fallback = ''): WorkerId {
  const raw = (job?.backend || '').trim().toLowerCase()
  if (raw === 'codex' || raw === 'openai' || raw === 'openai-codex') return 'codex'
  if (raw === 'opencode' || raw === 'open-code' || raw === 'omo') return 'opencode'
  const sid = (job?.opencode_session_id || '').trim()
  if (sid.startsWith('ses_')) return 'opencode'
  if (sid.includes('-') && sid.length >= 16) return 'codex'
  const desc = job?.description || ''
  if (/\bbackend\s*:\s*(openai-)?codex\b/i.test(desc) || /\bworker\s*:\s*codex\b/i.test(desc)) {
    return 'codex'
  }
  if (/\bbackend\s*:\s*opencode\b/i.test(desc)) return 'opencode'
  const fb = fallback.trim().toLowerCase()
  return fb === 'codex' ? 'codex' : 'opencode'
}

export function workerLabel(id: WorkerId): string {
  return id === 'codex' ? 'Codex' : 'OpenCode'
}

export function sessionKindLabel(id: WorkerId): string {
  return id === 'codex' ? 'thread' : 'session'
}
