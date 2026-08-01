/** Lightweight path helpers for the ops dashboard (no router library). */

export type AppRoute =
  | { kind: 'tasks' }
  | { kind: 'poll' }
  | { kind: 'settings' }
  | { kind: 'task'; issueKey: string; jobId: string | null }

export function parseLocation(
  pathname: string = window.location.pathname,
  search: string = window.location.search,
): AppRoute {
  const path = (pathname || '/').replace(/\/+$/, '') || '/'
  const params = new URLSearchParams(search)

  if (path === '/poll') return { kind: 'poll' }
  if (path === '/settings') return { kind: 'settings' }

  const taskMatch = path.match(/^\/tasks\/([^/]+)$/i)
  if (taskMatch) {
    const issueKey = decodeURIComponent(taskMatch[1]).toUpperCase()
    const jobId = params.get('job')
    return { kind: 'task', issueKey, jobId: jobId?.trim() || null }
  }

  // `/`, `/jobs`, and unknown paths fall back to jobs list
  return { kind: 'tasks' }
}

export function pathForTab(tab: 'tasks' | 'poll' | 'settings'): string {
  if (tab === 'poll') return '/poll'
  if (tab === 'settings') return '/settings'
  return '/jobs'
}

export function pathForTask(issueKey: string, jobId?: string | null): string {
  const base = `/tasks/${encodeURIComponent(issueKey.trim().toUpperCase())}`
  if (jobId?.trim()) {
    return `${base}?job=${encodeURIComponent(jobId.trim())}`
  }
  return base
}

/** Push a new history entry (user navigated in the app). */
export function navigateTo(path: string, replace = false): void {
  const method = replace ? 'replaceState' : 'pushState'
  window.history[method](null, '', path)
}

export function tabFromRoute(route: AppRoute): 'tasks' | 'poll' | 'settings' {
  if (route.kind === 'poll') return 'poll'
  if (route.kind === 'settings') return 'settings'
  return 'tasks'
}
