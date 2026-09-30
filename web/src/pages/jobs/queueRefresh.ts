/** Queue rows load with the Queue tab. Other tabs only warm the cache when the count changes. */

export function shouldLoadQueueForFilter(statusFilter: string): boolean {
  return statusFilter === 'queue'
}

export function shouldRefreshQueueList(
  statusFilter: string,
  liveQueued: number,
  lastFetchedQueued: number,
): boolean {
  if (shouldLoadQueueForFilter(statusFilter)) return false
  return liveQueued !== lastFetchedQueued
}

/** Waiting rows, newest first. Shared by the tab and the startup cache. */
export function waitingQueueRows<T extends { status?: string; created_at?: string | null }>(
  items: T[] | null | undefined,
): T[] {
  const rows = (items || []).filter((row) => row.status === 'queued')
  rows.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
  return rows
}

/** Do not say the queue is empty before the first response. Cached rows skip this. */
export function queuePlaceholder(
  ready: boolean,
  rowCount: number,
): 'loading' | 'empty' | 'rows' {
  if (rowCount > 0) return 'rows'
  if (!ready) return 'loading'
  return 'empty'
}
