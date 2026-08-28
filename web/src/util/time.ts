import { useEffect, useMemo, useState } from 'react'
import { IN_FLIGHT_STATUSES } from './status'

export function parseTimeMs(iso: string | null | undefined): number | null {
  if (!iso || !String(iso).trim()) return null
  const t = Date.parse(String(iso).trim())
  return Number.isFinite(t) ? t : null
}

/** Live local date + time for the dashboard chrome. */
export function formatDashboardClock(ms: number = Date.now()): string {
  try {
    return new Date(ms).toLocaleString(undefined, {
      weekday: 'short',
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return ''
  }
}

/** Local wall-clock label for chat bubbles (ISO from API is UTC). */
export function formatChatTime(iso: string | null | undefined): string {
  const ms = parseTimeMs(iso)
  if (ms == null) return ''
  try {
    return new Date(ms).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return String(iso)
  }
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

/** Split ``YYYY-MM-DDTHH:mm`` into date + 24-hour ``HH:mm``. */
export function splitDatetimeLocal(local: string): { date: string; time: string } {
  const raw = (local || '').trim()
  const m = raw.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
  if (!m) return { date: '', time: '' }
  return { date: m[1], time: m[2] }
}

/** Keep only a 24-hour ``HH:mm`` clock (rejects 12-hour am/pm text). */
export function normalizeTime24h(raw: string): string {
  const t = (raw || '').trim()
  const m = t.match(/^(\d{1,2}):(\d{2})$/)
  if (!m) return ''
  const h = Number(m[1])
  const min = Number(m[2])
  if (!Number.isInteger(h) || !Number.isInteger(min)) return ''
  if (h < 0 || h > 23 || min < 0 || min > 59) return ''
  return `${String(h).padStart(2, '0')}:${String(min).padStart(2, '0')}`
}

export function joinDatetimeLocal(date: string, time: string): string {
  const d = (date || '').trim()
  const t = normalizeTime24h(time)
  if (!d || !t) return ''
  return `${d}T${t}`
}

/** Schedule list label: ``2026-08-28 14:30`` (24-hour, no am/pm). */
export function formatScheduleWhen(iso: string | null | undefined): string {
  const raw = (iso || '').trim()
  if (!raw) return '—'
  const { date, time } = splitDatetimeLocal(raw)
  if (date && time) return `${date} ${time}`
  return raw.replace('T', ' ').replace(/:\d{2}$/, '').replace(/Z$/, '')
}

/** Local wall-clock now as naive ISO (same convention as scheduled_at). */
export function localNaiveNowIso(now: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}` +
    `T${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`
  )
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
