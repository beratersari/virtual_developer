/** Queue rows must load when the tab opens, not only on the throttled jobs reload. */

export function shouldRefreshQueueList(
  statusFilter: string,
  liveQueued: number,
  lastFetchedQueued: number,
): boolean {
  if (statusFilter === 'queue') return true
  return liveQueued !== lastFetchedQueued
}
