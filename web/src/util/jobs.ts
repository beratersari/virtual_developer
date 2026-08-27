import type { JobItem } from '../api/types'

/** Created/started stamp for list order. Jobs are not grouped by issue key. */
export function jobCreatedStamp(job: Pick<JobItem, 'started_at' | 'updated_at'>): string {
  return String(job.started_at || job.updated_at || '')
}

/** Newest created date first. Live runs stay at the top of the list. */
export function sortJobsByCreatedAt<T extends Pick<JobItem, 'job_id' | 'live' | 'started_at' | 'updated_at'>>(
  jobs: T[],
): T[] {
  return [...jobs].sort((a, b) => {
    const liveA = a.live ? 1 : 0
    const liveB = b.live ? 1 : 0
    if (liveA !== liveB) return liveB - liveA
    const byDate = jobCreatedStamp(b).localeCompare(jobCreatedStamp(a))
    if (byDate !== 0) return byDate
    return String(a.job_id || '').localeCompare(String(b.job_id || ''))
  })
}
