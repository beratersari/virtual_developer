import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Alert } from '../ui/Alert'
import { formatChatTime, formatDashboardClock, useNow } from '../util/time'
import { useLive } from './live'

const NAV = [
  { to: '/jobs', label: 'Jobs', match: (p: string) => p.startsWith('/jobs') || p.startsWith('/tasks') },
  { to: '/queue', label: 'Queue', match: (p: string) => p.startsWith('/queue') },
  { to: '/scheduled', label: 'Scheduled', match: (p: string) => p.startsWith('/scheduled') },
  { to: '/sessions', label: 'Sessions', match: (p: string) => p.startsWith('/sessions') },
  { to: '/poll', label: 'Board', match: (p: string) => p.startsWith('/poll') },
  { to: '/settings', label: 'Settings', match: (p: string) => p.startsWith('/settings') },
] as const

function IconJobs() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="2" y="3" width="12" height="3" rx="1" fill="currentColor" opacity="0.9" />
      <rect x="2" y="8" width="12" height="2" rx="1" fill="currentColor" opacity="0.55" />
      <rect x="2" y="12" width="8" height="2" rx="1" fill="currentColor" opacity="0.35" />
    </svg>
  )
}
function IconQueue() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="2" y="2.5" width="12" height="2.5" rx="1" fill="currentColor" opacity="0.9" />
      <rect x="2" y="6.75" width="12" height="2.5" rx="1" fill="currentColor" opacity="0.55" />
      <rect x="2" y="11" width="8" height="2.5" rx="1" fill="currentColor" opacity="0.35" />
    </svg>
  )
}
function IconClock() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 4.5V8l2.5 1.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}
function IconBoard() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="2" y="2.5" width="4" height="11" rx="1" fill="currentColor" opacity="0.9" />
      <rect x="7" y="2.5" width="3" height="7" rx="1" fill="currentColor" opacity="0.55" />
      <rect x="11" y="2.5" width="3" height="9" rx="1" fill="currentColor" opacity="0.35" />
    </svg>
  )
}
function IconSession() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <rect x="3" y="2.5" width="10" height="3" rx="1" fill="currentColor" opacity="0.9" />
      <rect x="3" y="6.5" width="10" height="3" rx="1" fill="currentColor" opacity="0.55" />
      <rect x="3" y="10.5" width="10" height="3" rx="1" fill="currentColor" opacity="0.35" />
    </svg>
  )
}
function IconGear() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="8" cy="8" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M8 2.2v1.4M8 12.4v1.4M2.2 8h1.4M12.4 8h1.4M3.9 3.9l1 1M11.1 11.1l1 1M12.1 3.9l-1 1M4.9 11.1l-1 1"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

const ICONS = [IconJobs, IconQueue, IconClock, IconSession, IconBoard, IconGear]

export function Shell() {
  const live = useLive()
  const location = useLocation()
  const now = useNow(true, 1000)
  const queued = live.poll?.will_process_count ?? 0
  const workQueued = live.queueQueued ?? 0
  const localClock = formatDashboardClock(now)
  const serverClock = formatChatTime(live.meta?.server_time)

  return (
    <div className="vd-app">
      <aside className="vd-sidebar">
        <div className="vd-brand">
          <div className="vd-mark">VD</div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold tracking-tight">Virtual Developer</div>
            <div className="text-[11px] text-text-muted">v{live.meta?.version ?? '—'}</div>
          </div>
        </div>

        <nav className="vd-nav" aria-label="Primary">
          {NAV.map((item, i) => {
            const Icon = ICONS[i]
            const active = item.match(location.pathname)
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={active ? 'active' : undefined}
                aria-current={active ? 'page' : undefined}
              >
                <Icon />
                <span className="flex-1">{item.label}</span>
                {item.to === '/poll' && queued > 0 && (
                  <span className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold text-[#1a0d08]">
                    {queued}
                  </span>
                )}
                {item.to === '/queue' && workQueued > 0 && (
                  <span className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-bold text-[#1a0d08]">
                    {workQueued}
                  </span>
                )}
              </NavLink>
            )
          })}
        </nav>

        <div className="mt-3 space-y-2 px-2 text-xs">
          <div className="hidden font-mono text-[11px] leading-snug text-text-secondary md:block">
            <div className="text-text">{localClock || '—'}</div>
            {serverClock && (
              <div className="mt-0.5 text-text-muted">Server {serverClock}</div>
            )}
          </div>
          <div className="hidden items-center gap-2 md:flex">
            <span
              className={`h-2 w-2 rounded-full ${
                live.connected ? 'vd-pulse bg-live' : 'bg-warning'
              }`}
            />
            <span className={live.connected ? 'text-success-text' : 'text-warning-text'}>
              {live.connected ? 'Connected' : 'Reconnecting'}
            </span>
          </div>
        </div>
      </aside>

      <main className="vd-main">
        <div className="vd-main-inner space-y-5">
          {live.error && <Alert>{live.error}</Alert>}
          {live.poll?.error && (
            <Alert tone="warning">
              <span className="font-medium">Poller: </span>
              {live.poll.error}
            </Alert>
          )}
          <div className="vd-page">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  )
}
