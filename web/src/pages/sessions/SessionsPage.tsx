import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { listPageFromSegment, withListPage } from '../../util/listPageUrl'
import { SessionWorkspacePage } from './SessionWorkspacePage'
import { fetchOpencodeWorkspaces, isAbortError } from '../../api/client'
import { noteLiveGeneration, usePageLoad } from '../../api/pageLoad'
import type { OpencodeWorkspaceItem, OpencodeWorkspaceList } from '../../api/types'
import { useLive } from '../../app/live'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

const PAGE_SIZE = 25

/** `/sessions/2` is a list page. `/sessions/osw_…` is a workspace. */
export function SessionsAtId() {
  const { workspaceId = '' } = useParams()
  if (listPageFromSegment(workspaceId)) return <SessionsPage />
  return <SessionWorkspacePage />
}

function kindChips(kinds: string[]): string {
  const labels = kinds.map((k) => (k === '' ? 'legacy' : k))
  return labels.join(' · ') || '—'
}

export function SessionsPage() {
  const live = useLive()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const page = listPageFromSegment(pathname.split('/').filter(Boolean)[1]) ?? 1
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [payload, setPayload] = useState<OpencodeWorkspaceList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const lastGenReload = useRef(0)
  const genSeen = useRef<number | null>(null)
  const flight = usePageLoad()
  const viewKey = `${page}|${debouncedQuery}`
  const [shownFor, setShownFor] = useState<string | null>(null)

  const queryRef = useRef(query)
  useEffect(() => {
    const t = window.setTimeout(() => {
      const changed = queryRef.current !== query
      queryRef.current = query
      setDebouncedQuery(query.trim())
      const onLaterPage = (listPageFromSegment(pathname.split('/').filter(Boolean)[1]) ?? 1) > 1
      if (changed && onLaterPage) navigate('/sessions')
    }, 250)
    return () => window.clearTimeout(t)
  }, [navigate, pathname, query])

  const reload = useCallback(async (mode: 'query' | 'tick', pageOverride?: number) => {
    const nextPage = pageOverride ?? page
    const key = `${nextPage}|${debouncedQuery}`
    const signal = mode === 'tick' ? flight.tick(key) : flight.query(key)
    if (!signal) return
    if (mode === 'query') setError(null)
    try {
      const p = await fetchOpencodeWorkspaces({
        page: nextPage,
        pageSize: PAGE_SIZE,
        q: debouncedQuery || undefined,
        signal,
      })
      if (signal.aborted) return
      setPayload(p)
      setShownFor(key)
      setError(null)
      const total = p.total ?? 0
      const size = p.page_size ?? PAGE_SIZE
      const pages = Math.max(1, Math.ceil(total / size) || 1)
      const landed = p.page ?? nextPage
      if (landed > pages) navigate(withListPage('/sessions', pages), { replace: true })
    } catch (e) {
      if (signal.aborted || isAbortError(e)) return
      if (mode === 'query') setPayload(null)
      setShownFor(key)
      setError(e instanceof Error ? e.message : 'Load failed')
    } finally {
      if (flight.settle(signal)) void reload('tick', nextPage)
    }
  }, [debouncedQuery, flight, navigate, page])

  useEffect(() => {
    void reload('query')
  }, [reload])
  useEffect(() => {
    if (!noteLiveGeneration(genSeen, live.generation)) return
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload('tick')
  }, [live.generation, reload])

  const awaiting = shownFor !== viewKey
  const rows: OpencodeWorkspaceItem[] = awaiting ? [] : payload?.workspaces || []
  const total = payload?.total ?? 0
  const currentPage = payload?.page ?? page
  const size = payload?.page_size ?? PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (currentPage - 1) * size + 1
  const to = Math.min(currentPage * size, total)

  return (
    <section className="space-y-5">
      <PageHeader
        title="Sessions"
        description="OpenCode chats for one repository, source, and target. Claude Code and Codex replies stay on the job Transcript tab."
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
          onClick={() => navigate(withListPage('/sessions', currentPage - 1))}
          className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
        >
          Prev
        </button>
        <button
          type="button"
          disabled={currentPage >= totalPages}
          onClick={() => navigate(withListPage('/sessions', currentPage + 1))}
          className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
        >
          Next
        </button>
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}

      <div className="space-y-2.5">
        {rows.length === 0 && (
          <div
            className="vd-panel px-5 py-10 text-center text-sm text-text-muted"
            aria-busy={awaiting && !error}
          >
            {awaiting && !error ? (
              <span className="inline-flex items-center gap-2">
                <Spinner /> Loading sessions…
              </span>
            ) : debouncedQuery ? (
              `No workspaces match "${debouncedQuery}".`
            ) : (
              'No OpenCode workspaces yet. A plan, build, or test run creates one. Claude Code and Codex jobs are listed under Jobs.'
            )}
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
