import { useEffect, useLayoutEffect, useState, type FormEvent, type ReactNode } from 'react'
import { fetchMeta } from '../api/client'
import { Spinner } from '../ui/Spinner'
import { ThemeSwitch } from '../ui/ThemeSwitch'
import { applyDocumentTitle } from '../util/pageTitle'
import {
  isUnauthorized,
  loginDashboard,
  setUnauthorizedHandler,
} from './dashboardAuth'

function GateBrand({ blurb }: { blurb?: string }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2.5">
        <div className="vd-mark" aria-hidden>
          <img src="/yaver-wink.gif?v=2" alt="" width={64} height={64} />
        </div>
        <div className="vd-kicker">Yaver</div>
      </div>
      {blurb ? <p className="font-mono text-sm text-text-secondary">{blurb}</p> : null}
    </div>
  )
}

function GateShell({ children }: { children: ReactNode }) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden px-4 py-10">
      <div className="absolute left-4 top-4 z-10">
        <ThemeSwitch />
      </div>
      {children}
    </div>
  )
}

function GateCard({ children }: { children: ReactNode }) {
  return (
    <div className="vd-gate-card relative w-full max-w-[400px] overflow-hidden border border-border-strong bg-surface">
      <div className="h-0.5 w-full bg-accent" />
      <div className="space-y-6 px-8 pb-8 pt-7">{children}</div>
    </div>
  )
}

export function DashboardAuthGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [needLogin, setNeedLogin] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [slow, setSlow] = useState(false)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useLayoutEffect(() => {
    if (!needLogin) return
    applyDocumentTitle('Sign in')
  }, [needLogin])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setNeedLogin(true)
      setReady(false)
    })
    return () => setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    let cancelled = false
    setFailed(false)
    setSlow(false)
    const slowTimer = window.setTimeout(() => {
      if (!cancelled) setSlow(true)
    }, 1_500)

    const probe = async () => {
      try {
        return await fetchMeta({ timeoutMs: 3_000 })
      } catch (err: unknown) {
        if (isUnauthorized(err)) throw err
        return await fetchMeta({ timeoutMs: 3_000 })
      }
    }

    void probe()
      .then((meta) => {
        if (cancelled) return
        if (meta.dashboard_auth && meta.authenticated === false) {
          setNeedLogin(true)
          setReady(false)
          return
        }
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
        setFailed(true)
        setSlow(true)
      })
      .finally(() => window.clearTimeout(slowTimer))
    return () => {
      cancelled = true
      window.clearTimeout(slowTimer)
    }
  }, [attempt])

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
      <GateShell>
        <form className="relative w-full max-w-[400px]" onSubmit={onSubmit}>
          <GateCard>
            <GateBrand />
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
          </GateCard>
        </form>
      </GateShell>
    )
  }

  if (!ready) {
    return (
      <GateShell>
        <GateCard>
          <GateBrand
            blurb={
              failed
                ? 'The API did not answer. Retry, or confirm the backend is running.'
                : slow
                  ? 'The API is taking longer than usual.'
                  : 'Checking this session.'
            }
          />
          <div className="flex items-center gap-3 border border-border bg-bg px-3.5 py-3">
            {!failed ? (
              <Spinner className="text-accent" />
            ) : (
              <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-warning" />
            )}
            <div className="min-w-0">
              <div className="text-sm font-medium text-text">
                {failed ? 'Could not reach the dashboard' : 'Opening console'}
              </div>
              <div className="text-xs text-text-muted">
                {failed
                  ? 'No response from /api/meta'
                  : slow
                    ? 'Waiting on the API…'
                    : 'One moment'}
              </div>
            </div>
          </div>
          {failed || slow ? (
            <button
              type="button"
              className="vd-btn vd-btn-secondary w-full justify-center py-2.5 text-sm font-semibold"
              onClick={() => setAttempt((n) => n + 1)}
            >
              Retry
            </button>
          ) : null}
        </GateCard>
      </GateShell>
    )
  }

  return <>{children}</>
}
