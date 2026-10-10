import type { JobItem } from '../api/types'

const ISSUE_KEY = /^[A-Za-z][A-Za-z0-9]*-\d+$/

/** Count label. An issue key is shown in uppercase. Other text stays as typed. */
export function jobsFilterEcho(filter: string): string {
  const text = filter.trim()
  if (!text) return ''
  if (ISSUE_KEY.test(text)) return text.toUpperCase()
  return text
}

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
