/** Status presentation — sparse semantics, neutral default. */

export type StatusTone = 'neutral' | 'info' | 'warning' | 'success' | 'danger'

export type StatusMeta = {
  label: string
  tone: StatusTone
}

const STATUS_MAP: Record<string, StatusMeta> = {
  pending: { label: 'Pending', tone: 'neutral' },
  planning: { label: 'Planning', tone: 'info' },
  plan_ready: { label: 'Plan ready', tone: 'info' },
  executing: { label: 'Executing', tone: 'warning' },
  running: { label: 'Running', tone: 'warning' },
  completed: { label: 'Completed', tone: 'success' },
  error: { label: 'Error', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
  unknown: { label: 'Unknown', tone: 'warning' },
  superseded: { label: 'Superseded', tone: 'neutral' },
}

export function statusMeta(status: string): StatusMeta {
  const key = (status || '').toLowerCase().replace(/\s+/g, '_')
  return (
    STATUS_MAP[key] || {
      label: status.replace(/_/g, ' ') || 'Unknown',
      tone: 'neutral',
    }
  )
}

/** Client-side job list filter groups. */
export type JobStatusFilter =
  | 'all'
  | 'live'
  | 'active'
  | 'error'
  | 'completed'
  | 'cancelled'

export function jobMatchesFilter(
  status: string,
  live: boolean,
  filter: JobStatusFilter,
): boolean {
  const s = (status || '').toLowerCase()
  switch (filter) {
    case 'all':
      return true
    case 'live':
      return live
    case 'active':
      return (
        live ||
        ['pending', 'planning', 'executing', 'running', 'plan_ready'].includes(s)
      )
    case 'error':
      return s === 'error' || s === 'unknown'
    case 'completed':
      return s === 'completed'
    case 'cancelled':
      return s === 'cancelled' || s === 'superseded'
    default:
      return true
  }
}
