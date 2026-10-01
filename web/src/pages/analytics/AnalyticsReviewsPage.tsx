import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { listPageFromSegment, withListPage } from '../../util/listPageUrl'
import { analyticsBackHref } from './analyticsPeriodUrl'
import { reviewsPageName } from '../../util/pageTitle'
import { ApiError, fetchAnalyticsReviews } from '../../api/client'
import type { AnalyticsReviewsPayload } from '../../api/types'
import { Alert } from '../../ui/Alert'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

const PAGE_SIZE = 25

const STATES: { id: string; label: string }[] = [
  { id: 'opened', label: 'Open' },
  { id: 'merged', label: 'Merged' },
  { id: 'closed', label: 'Closed' },
  { id: 'all', label: 'All' },
]

const ORIGINS: { id: string; label: string }[] = [
  { id: 'ours', label: 'Opened by us' },
  { id: 'contributed', label: 'Contributed' },
  { id: 'all', label: 'All' },
]

function fallbackLabel(row: {
  url: string
  gitlab_project: string
  gitlab_mr_iid: number | null
  azure_project: string
  azure_pr_id: number | null
}) {
  if (row.url) return row.url
  if (row.gitlab_project && row.gitlab_mr_iid) {
    return `${row.gitlab_project}!${row.gitlab_mr_iid}`
  }
  if (row.azure_project && row.azure_pr_id) {
    return `${row.azure_project} PR ${row.azure_pr_id}`
  }
  return '(no URL)'
}

export function AnalyticsReviewsPage() {
  const { page: pageParam = '' } = useParams()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const state = params.get('state') || 'all'
  const origin = params.get('origin') || 'all'
  const pathPage = listPageFromSegment(pageParam)
  const queryPage = listPageFromSegment(params.get('page') || '')
  const page = pathPage ?? queryPage ?? 1
  const [payload, setPayload] = useState<AnalyticsReviewsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const reqId = useRef(0)

  const filterOpts = {
    period: params.get('period') || '30d',
    from: params.get('from') || undefined,
    to: params.get('to') || undefined,
    status: params.get('status') || undefined,
    category: params.get('category') || undefined,
    source: params.get('source') || undefined,
    model: params.get('model') || undefined,
    backend: params.get('backend') || undefined,
    agent: params.get('agent') || undefined,
    repository: params.get('repository') || undefined,
  }

  const load = useCallback(async () => {
    const req = ++reqId.current
    setLoading(true)
    try {
      const data = await fetchAnalyticsReviews({
        state,
        origin,
        page,
        pageSize: PAGE_SIZE,
        ...filterOpts,
      })
      if (req !== reqId.current) return
      setPayload(data)
      setError(null)
    } catch (e) {
      if (req !== reqId.current) return
      setError(e instanceof ApiError || e instanceof Error ? e.message : 'Load failed')
    } finally {
      if (req === reqId.current) setLoading(false)
    }
    // filterOpts fields are read from params; listing `params` is enough.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, origin, page, params])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (pathPage || !queryPage) return
    const p = new URLSearchParams(params)
    p.delete('page')
    const search = p.toString()
    navigate(
      { pathname: withListPage('/analytics/reviews', queryPage), search: search ? `?${search}` : '' },
      { replace: true },
    )
  }, [navigate, params, pathPage, queryPage])

  const reviewsPath = (nextPage: number, search: URLSearchParams) => {
    search.delete('page')
    const q = search.toString()
    const path = withListPage('/analytics/reviews', nextPage)
    return q ? `${path}?${q}` : path
  }

  const setState = (next: string) => {
    const p = new URLSearchParams(params)
    p.set('state', next)
    navigate(reviewsPath(1, p))
  }

  const setOrigin = (next: string) => {
    const p = new URLSearchParams(params)
    p.set('origin', next)
    navigate(reviewsPath(1, p))
  }

  const setPage = (next: number) => {
    navigate(reviewsPath(next, new URLSearchParams(params)))
  }

  const backTo = analyticsBackHref(params.toString())

  const total = payload?.total ?? 0
  const size = payload?.page_size ?? PAGE_SIZE
  const currentPage = payload?.page ?? page
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (currentPage - 1) * size + 1
  const to = Math.min(currentPage * size, total)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Workbench"
        title={reviewsPageName(origin, state)}
        description={
          <>
            Unique GitLab MRs and Azure PRs from ticket work and /yaver
            follow-ups. Opened by us = Yaver created the MR from a ticket.
            Contributed = a /yaver follow-up on an existing MR or PR.{' '}
            <Link to={backTo} className="text-accent hover:underline">
              Back to Analytics
            </Link>
          </>
        }
        actions={
          loading ? (
            <span className="inline-flex items-center gap-2 text-xs text-text-muted">
              <Spinner /> Loading
            </span>
          ) : (
            <span className="text-xs text-text-muted">
              {from}–{to} of {total}
            </span>
          )
        }
      />

      {error && <Alert>{error}</Alert>}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
          {ORIGINS.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setOrigin(s.id)}
              className={`rounded-full px-3 py-1 text-xs font-semibold ${
                origin === s.id
                  ? 'bg-accent text-[#1a0d08]'
                  : 'text-text-secondary hover:text-text'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
          {STATES.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setState(s.id)}
              className={`rounded-full px-3 py-1 text-xs font-semibold ${
                state === s.id
                  ? 'bg-accent text-[#1a0d08]'
                  : 'text-text-secondary hover:text-text'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        </div>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <button
            type="button"
            disabled={currentPage <= 1}
            onClick={() => setPage(Math.max(1, currentPage - 1))}
            className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
          >
            Prev
          </button>
          <button
            type="button"
            disabled={currentPage >= totalPages}
            onClick={() => setPage(currentPage + 1)}
            className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
          >
            Next
          </button>
        </div>
      </div>

      <div className="vd-card overflow-hidden">
        {!payload && loading ? (
          <p className="px-4 py-6 text-sm text-text-muted">Loading…</p>
        ) : !(payload?.items || []).length ? (
          <p className="px-4 py-6 text-sm text-text-muted">
            No merge requests in this filter.
          </p>
        ) : (
          <div className="vd-table-wrap">
            <table className="vd-table vd-table-compact">
              <thead>
                <tr>
                  <th>State</th>
                  <th>Kind</th>
                  <th>Merge request</th>
                  <th>Issue</th>
                  <th>Jobs</th>
                </tr>
              </thead>
              <tbody>
                {(payload?.items || []).map((row, i) => {
                  const label = fallbackLabel(row)
                  return (
                    <tr key={`${row.url || label}-${i}`}>
                      <td className="capitalize">{row.state}</td>
                      <td>
                        {row.origin === 'ours' ? 'Opened by us' : 'Contributed'}
                      </td>
                      <td className="max-w-xl truncate font-mono text-xs">
                        {row.url ? (
                          <a
                            href={row.url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-accent hover:underline"
                          >
                            {label}
                          </a>
                        ) : (
                          <span>{label}</span>
                        )}
                        {row.title ? (
                          <div className="truncate text-[11px] text-text-muted">
                            {row.title}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        {row.issue_key ? (
                          <Link
                            to={`/tasks/${encodeURIComponent(row.issue_key)}`}
                            className="text-accent hover:underline"
                          >
                            {row.issue_key}
                          </Link>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="font-mono">{row.jobs}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
