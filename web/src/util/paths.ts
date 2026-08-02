/** Normalize path separators and match job artifacts across OS path styles. */

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
  // e.g. KAN-1_2024.log → KAN-1_2024 ; foo.prompt.txt → foo
  if (base.endsWith('.prompt.txt')) return base.slice(0, -'.prompt.txt'.length)
  const i = base.lastIndexOf('.')
  return i > 0 ? base.slice(0, i) : base
}

/** True when two filesystem paths refer to the same artifact (absolute, relative, or basename). */
export function pathsMatch(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false
  const na = normalizePath(a)
  const nb = normalizePath(b)
  if (na === nb) return true
  if (na.endsWith(nb) || nb.endsWith(na)) return true
  const ba = pathBasename(na)
  const bb = pathBasename(nb)
  if (ba === bb) return true
  // stem equality for sibling .log / .prompt.txt pairs is handled by caller
  return false
}

export function findByPath<T extends { path: string }>(
  items: T[],
  targetPath?: string | null,
): T | undefined {
  if (!targetPath) return undefined
  return items.find((item) => pathsMatch(item.path, targetPath))
}

/** Match prompt files that share a stem with a session log path. */
export function findPromptForJobPath<T extends { path: string; name?: string }>(
  prompts: T[],
  promptPath?: string | null,
  sessionLogPath?: string | null,
): T | undefined {
  if (promptPath) {
    const direct = findByPath(prompts, promptPath)
    if (direct) return direct
  }
  // Derive from session log: FOO.log → FOO.prompt.txt
  if (sessionLogPath) {
    const stem = pathStem(sessionLogPath)
    const byStem = prompts.find((p) => {
      const pStem = pathStem(p.path)
      return pStem === stem || pathBasename(p.path) === `${stem}.prompt.txt`
    })
    if (byStem) return byStem
  }
  return undefined
}

export function findLogForJobPath<T extends { path: string }>(
  logs: T[],
  sessionLogPath?: string | null,
): T | undefined {
  return findByPath(logs, sessionLogPath)
}
