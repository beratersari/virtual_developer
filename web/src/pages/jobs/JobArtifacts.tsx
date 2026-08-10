import { useEffect, useMemo, useState } from 'react'
import type { JobItem, JobRetryAttempt, TextArtifact } from '../../api/types'
import {
  findLogForJobPath,
  findPromptForJobPath,
  jobSessionPaths,
  pathBasename,
  sessionLogRetryLabel,
  sessionLogSortKey,
} from '../../util/paths'
import { PromptBlock } from '../../ui/PromptBlock'

export function JobPromptTab({
  job,
  prompts,
}: {
  job: JobItem
  prompts: TextArtifact[]
}) {
  const match = findPromptForJobPath(prompts, job.prompt_path, job.session_log_path)
  return (
    <div className="space-y-3 text-sm">
      <p className="text-xs text-text-muted">Exact text sent to the agent for this job.</p>
      {match ? (
        <PromptBlock
          highlight
          title={`Prompt · ${job.job_id}${match.truncated ? ' (truncated)' : ''}`}
          meta={pathBasename(match.path)}
          body={match.content || match.error || '(empty)'}
        />
      ) : (
        <div className="vd-alert vd-alert-warning">
          {job.prompt_path || job.session_log_path
            ? `Could not load prompt file${job.prompt_path ? ` (${pathBasename(job.prompt_path)})` : ''}.`
            : 'No prompt_path on this job record yet.'}
        </div>
      )}
    </div>
  )
}

export function JobSessionTab({
  job,
  sessionLogs,
}: {
  job: JobItem
  sessionLogs: TextArtifact[]
}) {
  const sessionPaths = useMemo(() => jobSessionPaths(job), [job])
  const entries = useMemo(() => {
    const retries: JobRetryAttempt[] = job.retry_attempts || []
    const pathSet = new Set(sessionPaths)
    const byBase = new Map<string, string>()
    for (const p of sessionPaths) byBase.set(pathBasename(p), p)
    for (const log of sessionLogs || []) {
      const base = pathBasename(log.path)
      if (byBase.has(base) && !pathSet.has(log.path)) pathSet.add(log.path)
    }
    const paths =
      pathSet.size > 0
        ? Array.from(pathSet).sort((a, b) => sessionLogSortKey(a) - sessionLogSortKey(b))
        : job.session_log_path
          ? [job.session_log_path]
          : []
    return paths.map((path, idx) => {
      const label = sessionLogRetryLabel(path)
      const match =
        findLogForJobPath(sessionLogs, path) ||
        findLogForJobPath(sessionLogs, byBase.get(pathBasename(path)) || null) ||
        (sessionLogs.length === 1 && paths.length === 1 ? sessionLogs[0] : undefined)
      const failedAs =
        retries.find(
          (r) =>
            r.failed_session_log_path &&
            (pathBasename(r.failed_session_log_path) === pathBasename(path) ||
              r.failed_session_log_path === path),
        ) || null
      return { path, label, index: idx, match, failedAs }
    })
  }, [sessionPaths, sessionLogs, job.session_log_path, job.retry_attempts])

  const sessionKey = entries.map((e) => e.path).join('|')
  const [openKeys, setOpenKeys] = useState<Record<string, boolean>>({})
  useEffect(() => {
    const paths = sessionKey ? sessionKey.split('|') : []
    const next: Record<string, boolean> = {}
    paths.forEach((p, i) => {
      next[p] = i === paths.length - 1
    })
    setOpenKeys(next)
  }, [sessionKey])

  const fallback =
    findLogForJobPath(sessionLogs, job.session_log_path) ||
    (sessionLogs || []).find((log) => (log.content || log.error || '').trim())
  if (entries.length === 0 && fallback) {
    return (
      <PromptBlock
        highlight
        title={`Session log · ${job.job_id}${fallback.truncated ? ' (truncated)' : ''}`}
        meta={pathBasename(fallback.path)}
        body={fallback.content || fallback.error || '(empty)'}
      />
    )
  }
  if (entries.length === 0) {
    return (
      <div className="vd-alert vd-alert-warning">
        {job.session_log_path
          ? `Could not load session log (${pathBasename(job.session_log_path)}).`
          : 'No OpenCode session logs linked to this job yet. Chat can still fill from the OpenCode database before a .log file is attached.'}
      </div>
    )
  }

  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-text-muted">OpenCode output. Latest attempt opens by default.</p>
        {entries.length > 1 && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="vd-btn-ghost text-[11px]"
              onClick={() => {
                const next: Record<string, boolean> = {}
                for (const e of entries) next[e.path] = true
                setOpenKeys(next)
              }}
            >
              Expand all
            </button>
            <button
              type="button"
              className="vd-btn-ghost text-[11px]"
              onClick={() => {
                const next: Record<string, boolean> = {}
                for (const e of entries) next[e.path] = false
                setOpenKeys(next)
              }}
            >
              Collapse all
            </button>
          </div>
        )}
      </div>
      <div className="space-y-2">
        {entries.map((entry) => {
          const isLatest = entry.index === entries.length - 1
          const isOpen = openKeys[entry.path] ?? isLatest
          const titleLabel = entry.label === 'initial' ? 'initial' : `_${entry.label}`
          const body = entry.match?.content || entry.match?.error || (entry.match ? '(empty)' : '')
          return (
            <details
              key={entry.path}
              className={`group rounded-lg border ${
                isLatest ? 'border-accent/40 bg-accent-muted/20' : 'border-border bg-bg'
              }`}
              open={isOpen}
              onToggle={(ev) => {
                const el = ev.currentTarget
                setOpenKeys((prev) => ({ ...prev, [entry.path]: el.open }))
              }}
            >
              <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-3 py-2.5 text-xs marker:content-none [&::-webkit-details-marker]:hidden">
                <span className="inline-block w-3 text-text-muted">▸</span>
                <span className="rounded border border-border bg-bg-elevated px-2 py-0.5 font-mono font-semibold">
                  {titleLabel}
                </span>
                {isLatest && (
                  <span className="text-[10px] font-semibold uppercase tracking-wide text-accent-text">
                    latest
                  </span>
                )}
                {entry.failedAs && (
                  <span className="text-danger-text">
                    failed → {entry.failedAs.reason || 'error'}
                  </span>
                )}
                <span className="min-w-0 flex-1 truncate font-mono text-text-muted">
                  {pathBasename(entry.path)}
                </span>
              </summary>
              <div className="border-t border-border">
                {entry.match ? (
                  <pre className="max-h-[min(60vh,36rem)] overflow-auto whitespace-pre-wrap p-4 font-mono text-xs leading-relaxed text-text">
                    {body}
                  </pre>
                ) : (
                  <div className="vd-alert vd-alert-warning m-3">
                    Could not load {pathBasename(entry.path)}
                  </div>
                )}
              </div>
            </details>
          )
        })}
      </div>
    </div>
  )
}
