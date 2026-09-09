import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { fetchMeta } from '../api/client'
import {
  isUnauthorized,
  loginDashboard,
  setUnauthorizedHandler,
} from './dashboardAuth'

export function DashboardAuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [needLogin, setNeedLogin] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setNeedLogin(true)
      setReady(false)
    })
    return () => setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    let cancelled = false
    void fetchMeta()
      .then(() => {
        if (cancelled) return
        setNeedLogin(false)
        setReady(true)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (isUnauthorized(err)) {
          setNeedLogin(true)
          setReady(false)
          return
        }
        setReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    void loginDashboard(username, password)
      .then(() => {
        setNeedLogin(false)
        setReady(true)
        setPassword('')
      })
      .catch((err: unknown) => {
        setError(isUnauthorized(err) ? 'Wrong username or password' : 'Could not reach the dashboard')
      })
      .finally(() => setBusy(false))
  }

  if (needLogin) {
    return (
      <div className="relative flex min-h-screen items-center justify-center overflow-hidden px-4 py-10">
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              'radial-gradient(900px 480px at 50% -10%, rgba(255,122,69,0.18), transparent 55%), radial-gradient(700px 420px at 100% 100%, rgba(91,157,255,0.10), transparent 50%)',
          }}
        />
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.07]"
          style={{
            backgroundImage:
              'linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px)',
            backgroundSize: '48px 48px',
          }}
        />

        <form
          className="relative w-full max-w-[400px] overflow-hidden rounded-2xl border border-border bg-surface/90 shadow-[0_24px_80px_rgba(0,0,0,0.45)] backdrop-blur-md"
          onSubmit={onSubmit}
        >
          <div className="h-1 w-full bg-accent" />
          <div className="space-y-6 px-8 pb-8 pt-7">
            <div className="space-y-2">
              <div className="flex items-center gap-2.5">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent-muted text-sm font-bold text-accent">
                  Y
                </span>
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-text-muted">
                    Yaver
                  </div>
                  <div className="text-lg font-semibold tracking-tight text-text">
                    Ops console
                  </div>
                </div>
              </div>
              <p className="text-sm text-text-secondary">
                Sign in to view jobs, storage, and settings.
              </p>
            </div>

            <div className="space-y-3.5">
              <label className="field !mb-0">
                <span>Username</span>
                <input
                  autoFocus
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </label>
              <label className="field !mb-0">
                <span>Password</span>
                <input
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
            </div>

            {error ? <p className="err">{error}</p> : null}

            <button
              type="submit"
              className="vd-btn vd-btn-primary w-full justify-center py-2.5 text-sm font-semibold"
              disabled={busy || !username || !password}
            >
              {busy ? 'Signing in…' : 'Sign in'}
            </button>

            <p className="text-center text-[11px] leading-relaxed text-text-muted">
              Board poller and GitLab webhooks do not use this login.
            </p>
          </div>
        </form>
      </div>
    )
  }

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-text-muted">
        Loading…
      </div>
    )
  }

  return <>{children}</>
}
