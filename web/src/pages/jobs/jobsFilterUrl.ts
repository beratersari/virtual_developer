import type { JobStatusFilter } from '../../util/status'
import { listPageFromSegment } from '../../util/listPageUrl'

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

/** Page on `/jobs/2` or `/jobs/queue/2`. A job id is not a page. */
export function jobsPageFromPath(pathname: string): number {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] !== 'jobs') return 1
  const n = listPageFromSegment(parts[parts.length - 1])
  if (!n) return 1
  if (parts.length === 2) return n
  if (parts.length === 3 && FILTER_FOR_SECTION[parts[1].toLowerCase()]) return n
  return 1
}
