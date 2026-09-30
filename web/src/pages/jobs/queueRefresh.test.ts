/**
 * Run: npx tsx src/pages/jobs/queueRefresh.test.ts
 */
import { queuePlaceholder, shouldLoadQueueForFilter, shouldRefreshQueueList, waitingQueueRows } from './queueRefresh'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(shouldLoadQueueForFilter('queue'), 'opening Queue loads the waiting list')
assert(!shouldLoadQueueForFilter('all'), 'other tabs do not block on the queue list')
assert(
  !shouldRefreshQueueList('queue', 0, 0),
  'the Queue tab does not use the count gate',
)
assert(
  shouldRefreshQueueList('all', 2, 0),
  'a new waiting row refreshes the cached list before the jobs throttle',
)
assert(
  !shouldRefreshQueueList('all', 1, 1),
  'other tabs do not refetch when the count is already loaded',
)
assert(queuePlaceholder(false, 0) === 'loading', 'first paint is loading, not an empty queue')
assert(queuePlaceholder(true, 0) === 'empty', 'a finished fetch with no rows is empty')
assert(queuePlaceholder(false, 2) === 'rows', 'cached rows show before the refresh returns')
const ordered = waitingQueueRows([
  { status: 'queued', created_at: '2020-01-01T00:00:00', queue_id: 'old' },
  { status: 'running', created_at: '2026-01-01T00:00:00', queue_id: 'run' },
  { status: 'queued', created_at: '2026-01-01T00:00:00', queue_id: 'new' },
])
assert(ordered.map((row) => row.queue_id).join(',') === 'new,old', 'newest waiting row is first')

console.log('queueRefresh.test.ts: ok')
