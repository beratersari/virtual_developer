import { useEffect, useMemo, useState } from 'react'
import { IN_FLIGHT_STATUSES } from './status'

export function parseTimeMs(iso: string | null | undefined): number | null {
  if (!iso || !String(iso).trim()) return null
  const t = Date.parse(String(iso).trim())
  return Number.isFinite(t) ? t : null
}

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

export function formatCountdown(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  const s = Math.max(0, seconds)
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}m ${r}s` : `${r}s`
}

/**
 * ``datetime-local`` value → naive local ISO for the scheduler.
 *
 * Do not use ``Date#toISOString()``: that emits UTC ``Z``, and ``list_due``
 * treats naive stamps as local wall clock (same as CLI / existing rows).
 */
export function datetimeLocalToNaiveIso(local: string): string {
  const raw = (local || '').trim()
  if (!raw) return raw
  return raw.length === 16 ? `${raw}:00` : raw
}

export function useNow(enabled = true, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [enabled, intervalMs])
  return now
}

export function useElapsedLabel(
  startedAt: string | null | undefined,
  completedAt: string | null | undefined,
  status: string,
  live: boolean,
): string {
  const tick =
    Boolean(startedAt) &&
    !completedAt &&
    (live || IN_FLIGHT_STATUSES.has((status || '').toLowerCase()))
  const now = useNow(tick)
  return useMemo(
    () => formatElapsedBetween(startedAt, completedAt, now),
    [startedAt, completedAt, now],
  )
}
