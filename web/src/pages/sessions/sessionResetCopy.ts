import { sessionKindGroup } from '../../util/sessions'

export function kindLabel(kind?: string | null): string {
  const group = sessionKindGroup(kind)
  if (group === 'plan') return 'plan'
  if (group === 'build') return 'build'
  if (group === 'test') return 'test'
  const raw = (kind || '').trim()
  return raw || 'legacy'
}

export function cloneFolder(dir?: string | null): string {
  const raw = (dir || '').trim()
  if (!raw) return ''
  const parts = raw.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || raw
}

export function multiRepoLabel(scope?: string | null): string {
  const raw = (scope || '').trim()
  if (!raw.startsWith('multi:')) return ''
  return raw.slice('multi:'.length).replace(/\|/g, ', ')
}

export function resetBody(session: {
  session_id: string
  kind?: string | null
  branch: string
  target_branch?: string
  scope?: string | null
  working_directory?: string | null
}): string {
  const kind = kindLabel(session.kind)
  const branch = `${session.branch}${session.target_branch ? ` → ${session.target_branch}` : ''}`
  const repos = multiRepoLabel(session.scope)
  const clone = cloneFolder(session.working_directory)
  const lines: string[] = []
  if (repos) lines.push(`Multi-repo: ${repos}.`)
  if (clone) lines.push(`Clone folder: ${clone}.`)
  lines.push(
    `The next ${kind} job that would have resumed ${session.session_id} starts a new session. Other sessions on ${branch} stay.`,
  )
  return lines.join('\n\n')
}
