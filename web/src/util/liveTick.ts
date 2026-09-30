/** Whether a WS envelope should refetch job/session lists. */

export function liveDataSignature(payload: {
  live_issue_keys?: string[]
  queue?: { queued_count?: number; epoch?: number }
}): string {
  const liveKeys = Array.isArray(payload.live_issue_keys)
    ? payload.live_issue_keys
        .map((k) => String(k || '').toUpperCase())
        .filter(Boolean)
        .sort()
        .join(',')
    : ''
  const q =
    payload.queue && typeof payload.queue.queued_count === 'number'
      ? String(payload.queue.queued_count)
      : ''
  const epoch =
    payload.queue && typeof payload.queue.epoch === 'number'
      ? String(payload.queue.epoch)
      : ''
  return `${liveKeys}|${q}|${epoch}`
}

export function shouldBumpLiveGeneration(
  payload: {
    type?: string
    jobs?: unknown
    tasks?: unknown
    live_issue_keys?: string[]
    queue?: { queued_count?: number; epoch?: number }
  },
  lastSig: string,
): { bump: boolean; sig: string } {
  const sig = liveDataSignature(payload)
  const jobsDirty =
    payload.type === 'dashboard' || payload.jobs != null || payload.tasks != null
  return { bump: jobsDirty || (Boolean(sig) && sig !== lastSig), sig }
}
