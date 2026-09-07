import type { OpencodeSessionBind } from '../api/types'

export type SessionKindGroup = 'plan' | 'build' | 'other'

export function sessionKindGroup(kind?: string | null): SessionKindGroup {
  const raw = (kind || '').trim().toLowerCase()
  if (raw === 'plan' || raw === 'planning' || raw === 'derman-plan') return 'plan'
  if (
    raw === 'build' ||
    raw === 'execution' ||
    raw === 'executing' ||
    raw === 'derman-build'
  ) {
    return 'build'
  }
  return 'other'
}

export function groupSessionBinds(rows: OpencodeSessionBind[]): {
  plan: OpencodeSessionBind[]
  build: OpencodeSessionBind[]
  other: OpencodeSessionBind[]
} {
  const plan: OpencodeSessionBind[] = []
  const build: OpencodeSessionBind[] = []
  const other: OpencodeSessionBind[] = []
  for (const row of rows) {
    const group = sessionKindGroup(row.kind)
    if (group === 'plan') plan.push(row)
    else if (group === 'build') build.push(row)
    else other.push(row)
  }
  return { plan, build, other }
}
