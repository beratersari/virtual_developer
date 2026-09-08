let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

export function notifyUnauthorized() {
  unauthorizedHandler?.()
}

export async function loginDashboard(username: string, password: string) {
  const res = await fetch('/api/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const err = new Error(res.status === 401 ? 'Wrong username or password' : 'Sign in failed')
    ;(err as Error & { status: number }).status = res.status
    throw err
  }
}

export function signOutDashboard() {
  // Flip the UI first. Waiting on /api/logout used to eat the click when the
  // dashboard thread pool was busy (Storage/GitLab scans, chat polls).
  unauthorizedHandler?.()
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), 4000)
  void fetch('/api/logout', {
    method: 'POST',
    credentials: 'include',
    signal: ctrl.signal,
  })
    .catch(() => {
      /* cookie clear is best-effort; the login gate is already showing */
    })
    .finally(() => window.clearTimeout(timer))
}

export function isUnauthorized(err: unknown): boolean {
  return Boolean(err && typeof err === 'object' && (err as { status?: number }).status === 401)
}
