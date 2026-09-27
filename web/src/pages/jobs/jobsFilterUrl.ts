import type { JobStatusFilter } from '../../util/status'

const SECTION_FOR_FILTER: Record<JobStatusFilter, string> = {
  all: '',
  live: 'in-flight',
  active: 'in-flight',
  queue: 'queue',
  error: 'error',
  completed: 'completed',
  cancelled: 'cancelled',
}

const FILTER_FOR_SECTION: Record<string, JobStatusFilter> = {
  'in-flight': 'active',
  active: 'active',
  queue: 'queue',
  error: 'error',
  completed: 'completed',
  cancelled: 'cancelled',
}

export function jobsFilterFromPath(pathname: string): JobStatusFilter {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] !== 'jobs' || parts.length < 2) return 'all'
  return FILTER_FOR_SECTION[parts[1].toLowerCase()] ?? 'all'
}

export function jobsFilterPath(filter: JobStatusFilter): string {
  const section = SECTION_FOR_FILTER[filter]
  return section ? `/jobs/${section}` : '/jobs'
}
