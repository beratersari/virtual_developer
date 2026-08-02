/** Lightweight path helpers for the ops dashboard (no router library). */

export type AppRoute =
  | { kind: 'tasks' }
  | { kind: 'poll' }
  | { kind: 'settings' }
  | { kind: 'scheduled' }
  | { kind: 'job'; jobId: string; issueKey: string | null }
  | { kind: 'task'; issueKey: string }

export function parseLocation(
  pathname: string = window.location.pathname,
  search: string = window.location.search,
): AppRoute {
  const path = (pathname || '/').replace(/\/+$/, '') || '/'
  const params = new URLSearchParams(search)

  if (path === '/poll') return { kind: 'poll' }
  if (path === '/settings') return { kind: 'settings' }
  if (path === '/scheduled' || path === '/schedules') return { kind: 'scheduled' }
  if (path === '/jobs' || path === '/') return { kind: 'tasks' }

  // Job detail: /jobs/{jobId}  (optional ?issue= for legacy ids)
  const jobMatch = path.match(/^\/jobs\/([^/]+)$/i)
  if (jobMatch) {
    const jobId = decodeURIComponent(jobMatch[1])
    const issueKey = (params.get('issue') || '').trim().toUpperCase() || null
    return { kind: 'job', jobId, issueKey }
  }

  // Task / issue detail: /tasks/{ISSUE_KEY}
  // Legacy: /tasks/{KEY}?job= → treat as job if job present
  const taskMatch = path.match(/^\/tasks\/([^/]+)$/i)
  if (taskMatch) {
    const issueKey = decodeURIComponent(taskMatch[1]).toUpperCase()
    const jobId = (params.get('job') || '').trim()
    if (jobId) {
      return { kind: 'job', jobId, issueKey }
    }
    return { kind: 'task', issueKey }
  }

  return { kind: 'tasks' }
}

export function pathForTab(
  tab: 'tasks' | 'poll' | 'settings' | 'scheduled',
): string {
  if (tab === 'poll') return '/poll'
  if (tab === 'settings') return '/settings'
  if (tab === 'scheduled') return '/scheduled'
  return '/jobs'
}

/** Issue / task page (Jira key lifecycle — not a single run). */
export function pathForTask(issueKey: string): string {
  return `/tasks/${encodeURIComponent(issueKey.trim().toUpperCase())}`
}

/** Single job / run page. */
export function pathForJob(jobId: string, issueKey?: string | null): string {
  const base = `/jobs/${encodeURIComponent(jobId.trim())}`
  if (issueKey?.trim()) {
    return `${base}?issue=${encodeURIComponent(issueKey.trim().toUpperCase())}`
  }
  return base
}

/** Push a new history entry (user navigated in the app). */
export function navigateTo(path: string, replace = false): void {
  const method = replace ? 'replaceState' : 'pushState'
  window.history[method](null, '', path)
}

export function tabFromRoute(
  route: AppRoute,
): 'tasks' | 'poll' | 'settings' | 'scheduled' {
  if (route.kind === 'poll') return 'poll'
  if (route.kind === 'settings') return 'settings'
  if (route.kind === 'scheduled') return 'scheduled'
  return 'tasks'
}
