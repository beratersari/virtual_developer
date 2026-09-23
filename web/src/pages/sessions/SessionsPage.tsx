import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchOpencodeWorkspaces } from '../../api/client'
import type { OpencodeWorkspaceItem, OpencodeWorkspaceList } from '../../api/types'
import { useLive } from '../../app/live'
import { PageHeader } from '../../ui/PageHeader'

const PAGE_SIZE = 25

function kindChips(kinds: string[]): string {
  const labels = kinds.map((k) => (k === '' ? 'legacy' : k))
  return labels.join(' · ') || '—'
}

export function SessionsPage() {
  const live = useLive()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [payload, setPayload] = useState<OpencodeWorkspaceList | null>(null)
  const [page, setPage] = useState(1)
  const [error, setError] = useState<string | null>(null)
  const lastGenReload = useRef(0)
  const reqId = useRef(0)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebouncedQuery(query.trim())
      setPage(1)
    }, 250)
    return () => window.clearTimeout(t)
  }, [query])

  const reload = useCallback(async (pageOverride?: number) => {
    const nextPage = pageOverride ?? page
    const req = ++reqId.current
    try {
      const p = await fetchOpencodeWorkspaces({
        page: nextPage,
        pageSize: PAGE_SIZE,
        q: debouncedQuery || undefined,
      })
      if (req !== reqId.current) return
      setPayload(p)
      setError(null)
      const total = p.total ?? 0
      const size = p.page_size ?? PAGE_SIZE
      const pages = Math.max(1, Math.ceil(total / size) || 1)
      const landed = p.page ?? nextPage
      if (landed > pages) setPage(pages)
    } catch (e) {
      if (req !== reqId.current) return
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }, [page, debouncedQuery])

  useEffect(() => {
    void reload()
  }, [reload])
  useEffect(() => {
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload()
  }, [live.generation, reload])

  const rows: OpencodeWorkspaceItem[] = payload?.workspaces || []
  const total = payload?.total ?? 0
  const currentPage = payload?.page ?? page
  const size = payload?.page_size ?? PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (currentPage - 1) * size + 1
  const to = Math.min(currentPage * size, total)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="OpenCode"
        title="Sessions"
        description="OpenCode chats for one repository + source + target. Claude Code and Codex replies are on the job Transcript tab, not in this list."
        actions={
          <label className="block text-xs text-text-muted">
            Search
            <input
              className="vd-input mt-1 w-64"
              placeholder="Repo, branch, or issue key"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        }
      />
      <div className="flex flex-wrap items-center justify-end gap-2 text-xs text-text-muted">
        <span>
          {from}–{to} of {total}
          {debouncedQuery ? ` · ${debouncedQuery}` : ''}
        </span>
        <button
          type="button"
          disabled={currentPage <= 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
        >
          Prev
        </button>
        <button
          type="button"
          disabled={currentPage >= totalPages}
          onClick={() => setPage((p) => p + 1)}
          className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
        >
          Next
        </button>
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}

      <div className="space-y-2.5">
        {rows.length === 0 && (
          <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
            {debouncedQuery
              ? `No workspaces match "${debouncedQuery}".`
              : 'No OpenCode workspaces yet. A plan, build, or test run creates one. Claude Code and Codex jobs are listed under Jobs.'}
          </div>
        )}
        {rows.map((w) => (
          <button
            key={w.workspace_id}
            type="button"
            className="vd-job w-full text-left"
            onClick={() =>
              navigate(`/sessions/${encodeURIComponent(w.workspace_id)}`)
            }
          >
            <div className="vd-job-bar tone-neutral" />
            <div className="min-w-0">
              <div className="font-mono text-xs text-text-muted">
                {w.repository_key || w.repository_url || '—'}
              </div>
              <div className="mt-0.5 font-mono text-sm font-semibold text-text">
                {w.branch}
                {w.target_branch ? ` → ${w.target_branch}` : ''}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-muted">
                <span>{kindChips(w.kinds)}</span>
                <span>
                  {w.session_count} session{w.session_count === 1 ? '' : 's'}
                </span>
                <span>
                  {w.job_count} job{w.job_count === 1 ? '' : 's'}
                </span>
                {w.issue_key ? <span>last {w.issue_key}</span> : null}
                {w.updated_at ? <span>{w.updated_at}</span> : null}
              </div>
            </div>
          </button>
        ))}
      </div>
    </section>
  )
}
