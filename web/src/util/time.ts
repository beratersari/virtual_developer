/** Display helpers for timestamps and elapsed duration (presentation only). */

export function parseTimeMs(iso: string | null | undefined): number | null {
  if (!iso || !String(iso).trim()) return null
  const t = Date.parse(String(iso).trim())
  return Number.isFinite(t) ? t : null
}

/** Whole seconds between start and end (or now if end omitted). */
export function elapsedSecondsBetween(
  startedAt: string | null | undefined,
  completedAt: string | null | undefined,
  nowMs: number = Date.now(),
): number | null {
  const start = parseTimeMs(startedAt)
  if (start == null) return null
  const end = completedAt ? parseTimeMs(completedAt) : nowMs
  if (end == null) return null
  return Math.max(0, Math.floor((end - start) / 1000))
}

/**
 * Compact human duration, e.g. ``45s``, ``3m 05s``, ``1h 02m 03s``.
 */
export function formatElapsedSeconds(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || !Number.isFinite(totalSeconds) || totalSeconds < 0) {
    return '—'
  }
  const s = Math.floor(totalSeconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) {
    return `${h}h ${String(m).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s`
  }
  if (m > 0) {
    return `${m}m ${String(sec).padStart(2, '0')}s`
  }
  return `${sec}s`
}

export function formatElapsedBetween(
  startedAt: string | null | undefined,
  completedAt: string | null | undefined,
  nowMs: number = Date.now(),
): string {
  return formatElapsedSeconds(elapsedSecondsBetween(startedAt, completedAt, nowMs))
}
