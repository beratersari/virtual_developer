/**
 * Run: npx tsx src/pages/jobs/queueRefresh.test.ts
 */
import { shouldRefreshQueueList } from './queueRefresh'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(shouldRefreshQueueList('queue', 0, 0), 'opening Queue fetches even when the count is unchanged')
assert(
  shouldRefreshQueueList('all', 2, 0),
  'a new waiting row refreshes the list before the jobs throttle',
)
assert(
  !shouldRefreshQueueList('all', 1, 1),
  'other tabs do not refetch when the count is already loaded',
)

console.log('queueRefresh.test.ts: ok')
