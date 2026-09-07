/**
 * Run: npx tsx src/util/jobs.test.ts
 */
import { sortJobsByCreatedAt } from './jobs'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const ordered = sortJobsByCreatedAt([
  { job_id: 'old-kan-1', live: false, started_at: '2026-08-01T10:00:00', updated_at: null },
  { job_id: 'new-kan-9', live: false, started_at: '2026-08-20T12:00:00', updated_at: null },
  { job_id: 'mid-kan-1', live: false, started_at: '2026-08-10T09:00:00', updated_at: null },
  { job_id: 'live-kan-2', live: true, started_at: '2026-08-02T08:00:00', updated_at: null },
])

assert(ordered[0].job_id === 'live-kan-2', 'live job first regardless of issue key')
assert(ordered[1].job_id === 'new-kan-9', 'newest created after live')
assert(ordered[2].job_id === 'mid-kan-1', 'middle created next')
assert(ordered[3].job_id === 'old-kan-1', 'oldest last — not grouped by KAN-1')

console.log('jobs.test.ts: ok')
