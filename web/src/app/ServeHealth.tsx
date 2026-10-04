import { createElement, useEffect, useState } from 'react'
import { fetchOpencodeServe, isAbortError, type OpencodeServeState } from '../api/client'

export type ServeHealthTone = 'healthy' | 'waiting' | 'down' | 'unknown'

export type ServeHealthView = {
  label: string
  tone: ServeHealthTone
  pulse: boolean
}

const VIEWS: Record<string, ServeHealthView> = {
  ready: { label: 'OpenCode healthy', tone: 'healthy', pulse: true },
  started: { label: 'OpenCode healthy', tone: 'healthy', pulse: true },
  reloaded: { label: 'OpenCode healthy', tone: 'healthy', pulse: true },
  reloading: { label: 'OpenCode restarting', tone: 'waiting', pulse: true },
  deferred: { label: 'OpenCode reload waiting', tone: 'waiting', pulse: false },
  running: { label: 'OpenCode not answering', tone: 'down', pulse: false },
  down: { label: 'OpenCode down', tone: 'down', pulse: false },
  failed: { label: 'OpenCode failed', tone: 'down', pulse: false },
}

export function serveHealthView(status: string | null | undefined): ServeHealthView {
  const key = (status || '').trim().toLowerCase()
  return (
    VIEWS[key] ?? {
      label: 'OpenCode unavailable',
      tone: 'down',
      pulse: false,
    }
  )
}

const DOT: Record<ServeHealthTone, string> = {
  healthy: 'h-2 w-2 shrink-0 rounded-full bg-live',
  waiting: 'h-2 w-2 shrink-0 rounded-full bg-warning',
  down: 'h-2 w-2 shrink-0 rounded-full bg-danger',
  unknown: 'h-2 w-2 shrink-0 rounded-full bg-text-muted',
}

const TEXT: Record<ServeHealthTone, string> = {
  healthy: 'text-success-text',
  waiting: 'text-warning-text',
  down: 'text-danger-text',
  unknown: 'text-text-muted',
}

type Phase = 'loading' | 'ready' | 'unreachable'

export function ServeHealthMark({
  status,
  message,
  phase = 'ready',
}: {
  status?: string
  message?: string
  phase?: Phase
}) {
  const view =
    phase === 'loading'
      ? { label: 'OpenCode checking', tone: 'unknown' as const, pulse: false }
      : phase === 'unreachable'
        ? {
            label: 'OpenCode unavailable',
            tone: 'down' as const,
            pulse: false,
          }
        : serveHealthView(status)
  const detail = (
    message ||
    (phase === 'unreachable' ? 'OpenCode serve status could not be read.' : '')
  ).trim()
  const title = detail || view.label
  const aria = detail && detail !== view.label ? `${view.label}. ${detail}` : view.label
  return createElement(
    'div',
    {
      className: 'mt-1 flex min-w-0 items-center gap-1.5',
      role: 'status',
      title,
      'aria-label': aria,
    },
    createElement('span', {
      className: view.pulse ? `vd-pulse ${DOT[view.tone]}` : DOT[view.tone],
      'aria-hidden': true,
    }),
    createElement(
      'span',
      { className: `text-[11px] leading-snug ${TEXT[view.tone]}` },
      view.label,
    ),
  )
}

export function ServeHealth() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [state, setState] = useState<OpencodeServeState | null>(null)

  useEffect(() => {
    let stop = false
    let inflight = false
    let timer = 0
    const current = { ctrl: null as AbortController | null }

    const tick = () => {
      if (stop || inflight) return
      inflight = true
      const ctrl = new AbortController()
      current.ctrl = ctrl
      void fetchOpencodeServe(ctrl.signal)
        .then((row) => {
          if (stop) return
          setState(row)
          setPhase('ready')
        })
        .catch((err: unknown) => {
          if (stop || isAbortError(err)) return
          setPhase('unreachable')
        })
        .finally(() => {
          inflight = false
        })
    }

    tick()
    timer = window.setInterval(tick, 4000)
    return () => {
      stop = true
      window.clearInterval(timer)
      current.ctrl?.abort()
    }
  }, [])

  return createElement(ServeHealthMark, {
    phase,
    status: state?.status,
    message:
      phase === 'unreachable'
        ? 'OpenCode serve status could not be read.'
        : state?.message,
  })
}
