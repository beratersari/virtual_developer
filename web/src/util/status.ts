/** Status presentation only. Cancel/delete hints must match backend rules. */

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
  queued: { label: 'Queued', tone: 'info' },
  completed: { label: 'Completed', tone: 'success' },
  error: { label: 'Error', tone: 'danger' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
  unknown: { label: 'Unknown', tone: 'warning' },
  superseded: { label: 'Superseded', tone: 'neutral' },
  scheduled: { label: 'Scheduled', tone: 'info' },
  dispatching: { label: 'Dispatching', tone: 'warning' },
  dispatched: { label: 'Dispatched', tone: 'success' },
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

export type JobStatusFilter =
  | 'all'
  | 'live'
  | 'active'
  | 'queue'
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
      // Same set as Active / In flight. Kept so old links do not go empty.
      return (
        live ||
        ['pending', 'planning', 'executing', 'running'].includes(s)
      )
    case 'active':
      return (
        live ||
        ['pending', 'planning', 'executing', 'running'].includes(s)
      )
    case 'queue':
      // Queue rows are not JobItem records — JobsPage handles this filter.
      return false
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

const LIVE_JOB_STATUSES = new Set([
  'running',
  'planning',
  'executing',
  'pending',
])

export const IN_FLIGHT_STATUSES = new Set([
  'pending',
  'planning',
  'executing',
  'running',
  'dispatching',
])

export function jobIsDeletable(status: string, live: boolean): boolean {
  if (live) return false
  return !LIVE_JOB_STATUSES.has((status || '').toLowerCase())
}

export function statusToneClass(status: string): string {
  return `tone-${statusMeta(status).tone}`
}

export function jobIsCancellable(status: string, live: boolean): boolean {
  const s = (status || '').toLowerCase()
  if (['completed', 'error', 'cancelled', 'superseded'].includes(s)) return false
  if (live) return true
  return LIVE_JOB_STATUSES.has(s)
}
