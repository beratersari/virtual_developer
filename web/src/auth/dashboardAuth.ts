let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler
}

export function notifyUnauthorized() {
  unauthorizedHandler?.()
}

export function isLoginRequiredResponse(status: number, body: unknown): boolean {
  if (status === 401) return true
  if (status !== 403) return false
  if (!body || typeof body !== 'object') return false
  const rec = body as { code?: unknown; detail?: unknown }
  return rec.code === 'login_required' || rec.detail === 'Dashboard login required'
}

export async function loginDashboard(username: string, password: string) {
  const res = await fetch('/api/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const failed = res.status === 401 || res.status === 403
    const err = new Error(failed ? 'Wrong username or password' : 'Sign in failed')
    ;(err as Error & { status: number }).status = failed ? 401 : res.status
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
