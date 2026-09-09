import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { dashboardWsUrl, fetchMeta, fetchPoll, fetchSettings } from '../api/client'
import type { Meta, PollPayload, SettingsPayload } from '../api/types'
import { shouldBumpLiveGeneration } from '../util/liveTick'
import { useNow } from '../util/time'
import { LiveContext, type LiveValue } from './live'

export function LiveProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [poll, setPoll] = useState<PollPayload | null>(null)
  const [settings, setSettings] = useState<SettingsPayload | null>(null)
  const [generation, setGeneration] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [queueQueued, setQueueQueued] = useState(0)
  const countdownRef = useRef<{ secs: number; atMs: number } | null>(null)
  const lastServerMs = useRef<number | null>(null)
  const lastLiveSig = useRef<string>('')

  const applyEnvelope = (payload: {
    type?: string
    meta?: Meta
    poll?: PollPayload
    settings?: SettingsPayload
    queue?: { queued_count?: number }
    live_issue_keys?: string[]
    jobs?: unknown
    tasks?: unknown
  }) => {
    const st = payload.poll?.server_time || payload.meta?.server_time || null
    const nextMs = st ? Date.parse(st) : Number.NaN
    if (
      lastServerMs.current != null &&
      !Number.isNaN(nextMs) &&
      nextMs + 2000 < lastServerMs.current
    ) {
      return
    }
    if (!Number.isNaN(nextMs)) lastServerMs.current = nextMs

    if (payload.meta) setMeta(payload.meta)
    if (payload.poll) {
      setPoll(payload.poll)
      if (typeof payload.poll.seconds_until_next_poll === 'number') {
        countdownRef.current = {
          secs: payload.poll.seconds_until_next_poll,
          atMs: Date.now(),
        }
      }
    }
    if (payload.settings) setSettings(payload.settings)
    if (payload.queue && typeof payload.queue.queued_count === 'number') {
      setQueueQueued(payload.queue.queued_count)
    }
    const tick = shouldBumpLiveGeneration(payload, lastLiveSig.current)
    if (tick.bump) {
      if (tick.sig) lastLiveSig.current = tick.sig
      setGeneration((g) => g + 1)
    }
    setError(null)
  }

  useEffect(() => {
    let cancelled = false
    void Promise.allSettled([fetchMeta(), fetchPoll(), fetchSettings()]).then(
      (results) => {
        if (cancelled) return
        const [m, p, s] = results
        if (m.status === 'fulfilled') setMeta(m.value)
        if (p.status === 'fulfilled') {
          setPoll(p.value)
          if (typeof p.value.seconds_until_next_poll === 'number') {
            countdownRef.current = {
              secs: p.value.seconds_until_next_poll,
              atMs: Date.now(),
            }
          }
        }
        if (s.status === 'fulfilled') setSettings(s.value)
        if (
          m.status === 'rejected' &&
          p.status === 'rejected' &&
          s.status === 'rejected'
        ) {
          setError('Dashboard API unreachable')
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let retry: number | undefined
    let attempt = 0

    const connect = () => {
      if (closed) return
      try {
        ws = new WebSocket(dashboardWsUrl())
        ws.onopen = () => {
          attempt = 0
          setConnected(true)
        }
        ws.onclose = () => {
          setConnected(false)
          if (closed) return
          const delay = Math.min(15_000, 2_000 * (1 + attempt))
          attempt += 1
          retry = window.setTimeout(connect, delay)
        }
        ws.onerror = () => setConnected(false)
        ws.onmessage = (ev) => {
          try {
            applyEnvelope(JSON.parse(ev.data) as {
              type?: string
              meta?: Meta
              poll?: PollPayload
              settings?: SettingsPayload
              queue?: { queued_count?: number }
              live_issue_keys?: string[]
              jobs?: unknown
              tasks?: unknown
            })
          } catch {
            /* ignore malformed frames */
          }
        }
      } catch {
        setConnected(false)
        const delay = Math.min(15_000, 2_000 * (1 + attempt))
        attempt += 1
        retry = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      closed = true
      if (retry) window.clearTimeout(retry)
      ws?.close()
    }
  }, [])

  const now = useNow(true, 1000)
  const pollCountdown = useMemo(() => {
    const snap = countdownRef.current
    if (snap) {
      const elapsed = Math.floor((now - snap.atMs) / 1000)
      return Math.max(0, snap.secs - elapsed)
    }
    return poll?.seconds_until_next_poll ?? null
  }, [now, poll?.seconds_until_next_poll])

  const value = useMemo<LiveValue>(
    () => ({
      connected,
      meta,
      poll,
      settings,
      generation,
      pollCountdown,
      error,
      queueQueued,
    }),
    [connected, meta, poll, settings, generation, pollCountdown, error, queueQueued],
  )

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}
