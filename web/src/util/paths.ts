/** Match job artifacts across OS path styles. Display-only. */

export function normalizePath(p: string): string {
  return p.replace(/\\/g, '/').replace(/\/+/g, '/')
}

export function pathBasename(p: string): string {
  const n = normalizePath(p)
  const parts = n.split('/')
  return parts[parts.length - 1] || n
}

export function pathStem(p: string): string {
  const base = pathBasename(p)
  if (base.endsWith('.prompt.txt')) return base.slice(0, -'.prompt.txt'.length)
  const i = base.lastIndexOf('.')
  return i > 0 ? base.slice(0, i) : base
}

export function pathsMatch(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false
  const na = normalizePath(a)
  const nb = normalizePath(b)
  if (na === nb) return true
  return pathBasename(na) === pathBasename(nb)
}

export function findByPath<T extends { path: string }>(
  items: T[],
  targetPath?: string | null,
): T | undefined {
  if (!targetPath) return undefined
  return items.find((item) => pathsMatch(item.path, targetPath))
}

export function findPromptForJobPath<T extends { path: string; name?: string }>(
  prompts: T[],
  promptPath?: string | null,
  sessionLogPath?: string | null,
): T | undefined {
  if (promptPath) {
    const direct = findByPath(prompts, promptPath)
    if (direct) return direct
  }
  if (sessionLogPath) {
    const stem = pathStem(sessionLogPath)
    return prompts.find((p) => {
      const pStem = pathStem(p.path)
      return pStem === stem || pathBasename(p.path) === `${stem}.prompt.txt`
    })
  }
  return undefined
}

export function findLogForJobPath<T extends { path: string }>(
  logs: T[],
  sessionLogPath?: string | null,
): T | undefined {
  return findByPath(logs, sessionLogPath)
}

export function sessionLogRetryLabel(pathOrName: string): string {
  const base = pathBasename(pathOrName)
  const m = base.match(/_retry(\d+)\.log$/i)
  if (m) return `retry${m[1]}`
  const legacy = base.match(/_(\d+)\.log$/i)
  if (legacy && !/_\d{8}_\d{6}\.log$/i.test(base)) {
    const n = Number(legacy[1])
    if (n > 0) return `retry${n}`
  }
  return 'initial'
}

export function jobSessionPaths(job: {
  session_log_path?: string | null
  session_log_paths?: string[] | null
  retry_attempts?: Array<{ failed_session_log_path?: string | null }> | null
}): string[] {
  const out: string[] = []
  const add = (p?: string | null) => {
    if (p && !out.includes(p)) out.push(p)
  }
  for (const p of job.session_log_paths || []) add(p)
  add(job.session_log_path)
  for (const r of job.retry_attempts || []) add(r.failed_session_log_path)
  return out
}

export function sessionLogSortKey(pathOrName: string): number {
  const label = sessionLogRetryLabel(pathOrName)
  if (label === 'initial') return 0
  const m = label.match(/^retry(\d+)$/i)
  return m ? Number(m[1]) : 999
}
