import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ApiError, fetchAnalytics } from '../../api/client'
import type {
  AnalyticsFacet,
  AnalyticsNamedCount,
  AnalyticsPayload,
} from '../../api/types'
import { useLive } from '../../app/live'
import { Alert } from '../../ui/Alert'
import { LineChart, type LineSeries } from '../../ui/LineChart'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'
import { jobsFilterPath } from '../jobs/jobsFilterUrl'
import {
  ANALYTICS_PERIOD_LABELS,
  ANALYTICS_PERIODS,
  analyticsPeriodFromParam,
  analyticsPeriodPath,
  type AnalyticsPeriod,
} from './analyticsPeriodUrl'

const PERIODS: { id: AnalyticsPeriod; label: string }[] = ANALYTICS_PERIODS.map((id) => ({
  id,
  label: ANALYTICS_PERIOD_LABELS[id],
}))

type SeriesKey = 'total' | 'completed' | 'error' | 'cancelled' | 'in_flight'

const OUTCOME_SERIES: { id: SeriesKey; label: string; color: string }[] = [
  { id: 'total', label: 'Total', color: 'var(--chart-total)' },
  { id: 'completed', label: 'Completed', color: 'var(--chart-completed)' },
  { id: 'error', label: 'Error', color: 'var(--chart-error)' },
  { id: 'cancelled', label: 'Cancelled', color: 'var(--chart-cancelled)' },
  { id: 'in_flight', label: 'In flight', color: 'var(--chart-inflight)' },
]

function csv(set: Set<string>) {
  return [...set].filter(Boolean).join(',')
}

function toggle<T extends string>(set: Set<T>, id: T): Set<T> {
  const next = new Set(set)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}

function localInputFromIso(iso: string) {
  if (!iso) return ''
  return iso.slice(0, 16)
}

function isoFromLocal(local: string) {
  if (!local) return ''
  return local.length === 16 ? `${local}:00` : local
}

/** Statuses that already have a count card under the filters. */
const STATUS_ON_CARDS = new Set(['completed', 'error', 'cancelled'])

function FacetGroup({
  title,
  items,
  selected,
  onChange,
  className = '',
  hideCountIds,
}: {
  title: string
  items: AnalyticsFacet[]
  selected: Set<string>
  onChange: (next: Set<string>) => void
  className?: string
  hideCountIds?: Set<string>
}) {
  if (!items.length) return null
  return (
    <div className={className}>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-text-muted">
          {title}
        </div>
        {selected.size > 0 && (
          <button
            type="button"
            className="vd-btn-ghost text-[11px]"
            onClick={() => onChange(new Set())}
          >
            Clear
          </button>
        )}
      </div>
      <div className="flex max-h-40 flex-col gap-0.5 overflow-y-auto pr-1">
        {items.map((item) => {
          const on = selected.has(item.id)
          return (
            <label
              key={item.id}
              className={`flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-xs ${
                on ? 'bg-accent-muted text-accent-text' : 'text-text-secondary hover:bg-surface-hover'
              }`}
            >
              <input
                type="checkbox"
                className="vd-checkbox"
                checked={on}
                onChange={() => onChange(toggle(selected, item.id))}
              />
              <span className="min-w-0 flex-1 truncate" title={item.label}>
                {item.label}
              </span>
              {!(hideCountIds && hideCountIds.has(item.id)) && (
                <span className="font-mono text-text-muted">{item.jobs}</span>
              )}
            </label>
          )
        })}
      </div>
    </div>
  )
}

function CountCard({
  label,
  value,
  tone,
  to,
}: {
  label: string
  value: number
  tone?: 'success' | 'danger' | 'muted'
  to?: string
}) {
  const color =
    tone === 'success'
      ? 'text-success-text'
      : tone === 'danger'
        ? 'text-danger-text'
        : tone === 'muted'
          ? 'text-text-muted'
          : 'text-text'
  const body = (
    <>
      <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-text-muted">
        {label}
      </div>
      <div className={`mt-1 font-mono text-2xl font-semibold ${color}`}>{value}</div>
    </>
  )
  if (to) {
    return (
      <Link
        to={to}
        className="vd-card block px-4 py-3 text-inherit no-underline hover:border-accent"
      >
        {body}
      </Link>
    )
  }
  return <div className="vd-card px-4 py-3">{body}</div>
}

function CategoryMix({ rows }: { rows: AnalyticsNamedCount[] }) {
  const top = rows.slice(0, 6)
  return (
    <div className="vd-card flex min-w-0 flex-col p-4">
      <h2 className="text-sm font-semibold">Category mix</h2>
      {top.length === 0 ? (
        <p className="py-8 text-center text-sm text-text-muted">No jobs in this filter.</p>
      ) : (
        <ul className="mt-4 flex flex-1 flex-col justify-center gap-3">
          {top.map((row) => (
            <li key={row.id}>
              <div className="mb-1 flex items-baseline justify-between gap-3 text-xs">
                <span className="min-w-0 truncate font-medium text-text" title={row.label || row.id}>
                  {row.label || row.id}
                </span>
                <span className="shrink-0 font-mono tabular-nums text-text-muted">
                  {row.jobs} · {row.share}%
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-bg" aria-hidden>
                <div
                  className="h-full rounded-full bg-accent"
                  style={{ width: `${Math.min(100, Math.max(0, row.share))}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function BreakdownTable({ rows }: { rows: AnalyticsNamedCount[] }) {
  if (!rows.length) {
    return <p className="px-4 py-6 text-sm text-text-muted">No jobs in this filter.</p>
  }
  return (
    <div className="vd-table-wrap">
      <table className="vd-table vd-table-compact vd-table-fit">
        <colgroup>
          <col />
          <col style={{ width: '4.25rem' }} />
          <col style={{ width: '6.25rem' }} />
          <col style={{ width: '4.25rem' }} />
          <col style={{ width: '5.5rem' }} />
          <col style={{ width: '4.75rem' }} />
          <col style={{ width: '8.5rem' }} />
        </colgroup>
        <thead>
          <tr>
            <th>Name</th>
            <th>Jobs</th>
            <th>Completed</th>
            <th>Error</th>
            <th>Cancelled</th>
            <th>In flight</th>
            <th>Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="font-medium" title={row.label || row.id}>
                {row.label || row.id}
              </td>
              <td className="font-mono">{row.jobs}</td>
              <td className="font-mono text-success-text">{row.completed}</td>
              <td className="font-mono text-danger-text">{row.error}</td>
              <td className="font-mono text-text-muted">{row.cancelled}</td>
              <td className="font-mono">{row.in_flight}</td>
              <td>
                <div className="flex items-center gap-2">
                  <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-bg">
                    <div
                      className="h-full rounded-full bg-accent"
                      style={{ width: `${Math.min(100, Math.max(0, row.share))}%` }}
                    />
                  </div>
                  <span className="w-12 shrink-0 text-right font-mono text-text-muted">
                    {row.share}%
                  </span>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function AnalyticsPage() {
  const { period: periodParam = '' } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const period = analyticsPeriodFromParam(periodParam) ?? '30d'
  const live = useLive()
  const [customFrom, setCustomFrom] = useState(() =>
    period === 'custom' ? localInputFromIso(searchParams.get('from') || '') : '',
  )
  const [customTo, setCustomTo] = useState(() =>
    period === 'custom' ? localInputFromIso(searchParams.get('to') || '') : '',
  )
  const [status, setStatus] = useState<Set<string>>(() => new Set())
  const [category, setCategory] = useState<Set<string>>(() => new Set())
  const [source, setSource] = useState<Set<string>>(() => new Set())
  const [model, setModel] = useState<Set<string>>(() => new Set())
  const [backend, setBackend] = useState<Set<string>>(() => new Set())
  const [agent, setAgent] = useState<Set<string>>(() => new Set())
  const [repository, setRepository] = useState<Set<string>>(() => new Set())
  const [visible, setVisible] = useState<Set<SeriesKey>>(
    () => new Set(['total', 'completed', 'error']),
  )
  const [payload, setPayload] = useState<AnalyticsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const reqId = useRef(0)
  const lastGenReload = useRef(0)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (periodParam && analyticsPeriodFromParam(periodParam) === null) {
      navigate(analyticsPeriodPath('30d'), { replace: true })
    }
  }, [navigate, periodParam])

  const load = useCallback(
    async (opts?: { quiet?: boolean }) => {
      // Live ticks must not abort an in-flight chart GET. Doing that on an
      // 8s cadence while aggregation takes longer looks like a hang (spinner
      // forever) or a false "Request timed out". Period and filter changes
      // still cancel the previous GET so requests do not stack.
      if (opts?.quiet && abortRef.current && !abortRef.current.signal.aborted) {
        return
      }
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac
      const req = ++reqId.current
      if (!opts?.quiet) setLoading(true)
      try {
        if (period === 'custom' && (!customFrom || !customTo)) return
        const data = await fetchAnalytics({
          period: period === 'custom' ? 'all' : period,
          from: period === 'custom' ? isoFromLocal(customFrom) : undefined,
          to: period === 'custom' ? isoFromLocal(customTo) : undefined,
          status: csv(status) || undefined,
          category: csv(category) || undefined,
          source: csv(source) || undefined,
          model: csv(model) || undefined,
          backend: csv(backend) || undefined,
          agent: csv(agent) || undefined,
          repository: csv(repository) || undefined,
          signal: ac.signal,
        })
        if (req !== reqId.current) return
        setPayload(data)
        setError(null)
      } catch (e) {
        if (req !== reqId.current || ac.signal.aborted) return
        if (!opts?.quiet) {
          const timedOut = e instanceof ApiError && e.status === 408
          setError(
            timedOut
              ? 'Analytics took too long. Try a shorter period or fewer filters.'
              : e instanceof Error
                ? e.message
                : 'Load failed',
          )
        }
      } finally {
        if (req === reqId.current) setLoading(false)
      }
    },
    [
      period,
      customFrom,
      customTo,
      status,
      category,
      source,
      model,
      backend,
      agent,
      repository,
    ],
  )

  useEffect(() => {
    void load()
    return () => abortRef.current?.abort()
  }, [load])

  useEffect(() => {
    const now = Date.now()
    if (now - lastGenReload.current < 8000) return
    lastGenReload.current = now
    void load({ quiet: true })
    // Reload on live ticks only. Including `load` here refetched on every
    // period click. Quiet loads skip while a chart GET is in flight
    // so a live tick cannot abort (and restack) a slow aggregation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.generation])

  const facets = payload?.facets || {}
  const labels = payload?.series.map((p) => p.label) || []
  const showCharts = visible.size > 0
  const outcomeSeries: LineSeries[] = useMemo(() => {
    if (!payload) return []
    return OUTCOME_SERIES.filter((s) => visible.has(s.id)).map((s) => ({
      id: s.id,
      label: s.label,
      color: s.color,
      values: payload.series.map((p) => p[s.id] || 0),
    }))
  }, [payload, visible])

  const resetFilters = () => {
    setStatus(new Set())
    setCategory(new Set())
    setSource(new Set())
    setModel(new Set())
    setBackend(new Set())
    setAgent(new Set())
    setRepository(new Set())
  }

  const filterActive =
    status.size +
      category.size +
      source.size +
      model.size +
      backend.size +
      agent.size +
      repository.size >
    0

  const reviewHref = (origin: string, state: string) => {
    const p = new URLSearchParams()
    p.set('origin', origin)
    p.set('state', state)
    if (period === 'custom') {
      p.set('period', 'all')
      if (customFrom) p.set('from', isoFromLocal(customFrom))
      if (customTo) p.set('to', isoFromLocal(customTo))
    } else {
      p.set('period', period)
    }
    if (csv(status)) p.set('status', csv(status))
    if (csv(category)) p.set('category', csv(category))
    if (csv(source)) p.set('source', csv(source))
    if (csv(model)) p.set('model', csv(model))
    if (csv(backend)) p.set('backend', csv(backend))
    if (csv(agent)) p.set('agent', csv(agent))
    if (csv(repository)) p.set('repository', csv(repository))
    return `/analytics/reviews?${p.toString()}`
  }

  return (
    <section className="space-y-5">
      <PageHeader
        title="Analytics"
        actions={
          loading ? (
            <span className="inline-flex items-center gap-2 text-xs text-text-muted">
              <Spinner /> Loading
            </span>
          ) : null
        }
      />

      {error && <Alert>{error}</Alert>}

      <div className="flex flex-wrap items-center gap-2">
        <div className="vd-seg">
          {PERIODS.map((p) => (
            <button
              key={p.id}
              type="button"
              aria-pressed={period === p.id}
              onClick={() => {
                if (p.id === 'custom') {
                  const start = payload?.range.start || ''
                  const end = payload?.range.end || ''
                  setCustomFrom((prev) => prev || localInputFromIso(start))
                  setCustomTo((prev) => prev || localInputFromIso(end))
                }
                navigate(analyticsPeriodPath(p.id))
              }}
              className={`vd-seg-btn ${period === p.id ? 'is-on' : ''}`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {period === 'custom' && (
        <div className="flex flex-wrap gap-3">
          <label className="text-xs text-text-muted">
            From
            <input
              type="datetime-local"
              className="vd-input mt-1"
              value={customFrom}
              onChange={(e) => setCustomFrom(e.target.value)}
            />
          </label>
          <label className="text-xs text-text-muted">
            To
            <input
              type="datetime-local"
              className="vd-input mt-1"
              value={customTo}
              onChange={(e) => setCustomTo(e.target.value)}
            />
          </label>
        </div>
      )}

      <div className="vd-card flex flex-wrap items-start gap-x-6 gap-y-4 p-4">
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Status"
          items={facets.status || []}
          selected={status}
          onChange={setStatus}
          hideCountIds={STATUS_ON_CARDS}
        />
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Category"
          items={facets.category || []}
          selected={category}
          onChange={setCategory}
        />
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Source"
          items={facets.source || []}
          selected={source}
          onChange={setSource}
        />
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Backend"
          items={facets.backend || []}
          selected={backend}
          onChange={setBackend}
        />
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Model"
          items={facets.model || []}
          selected={model}
          onChange={setModel}
        />
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Agent"
          items={facets.agent || []}
          selected={agent}
          onChange={setAgent}
        />
        <FacetGroup
          className="min-w-52 flex-1 basis-56"
          title="Repository"
          items={facets.repository || []}
          selected={repository}
          onChange={setRepository}
        />
        {filterActive && (
          <div className="flex items-end">
            <button type="button" className="vd-btn vd-btn-secondary" onClick={resetFilters}>
              Reset filters
            </button>
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <CountCard label="Total jobs" value={payload?.totals.jobs ?? 0} />
        <CountCard
          label="Completed"
          value={payload?.totals.completed ?? 0}
          tone="success"
          to={jobsFilterPath('completed')}
        />
        <CountCard
          label="Error"
          value={payload?.totals.error ?? 0}
          tone="danger"
          to={jobsFilterPath('error')}
        />
        <CountCard
          label="Cancelled"
          value={payload?.totals.cancelled ?? 0}
          tone="muted"
          to={jobsFilterPath('cancelled')}
        />
        <CountCard label="In flight" value={payload?.totals.in_flight ?? 0} />
        <CountCard
          label="Models used"
          value={(payload?.models || []).filter((m) => m.id !== '(unset)').length}
        />
      </div>

      <div className="vd-analytics-chart">
      <div className="vd-card min-w-0 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">Jobs over time</h2>
          <div className="flex flex-wrap gap-3 text-xs">
            {OUTCOME_SERIES.map((s) => (
              <label key={s.id} className="inline-flex items-center gap-1.5 text-text-secondary">
                <input
                  type="checkbox"
                  className="vd-checkbox"
                  checked={visible.has(s.id)}
                  onChange={() => setVisible(toggle(visible, s.id))}
                />
                <span className="inline-block h-2 w-2 rounded-full" style={{ background: s.color }} />
                {s.label}
              </label>
            ))}
          </div>
        </div>
        {!showCharts ? (
          <p className="py-8 text-center text-sm text-text-muted">
            Select a series to show the chart.
          </p>
        ) : outcomeSeries.some((s) => s.values.some((v) => v > 0)) ? (
          <LineChart
            labels={labels}
            series={outcomeSeries}
            height={200}
            label="Jobs over time"
          />
        ) : (
          <p className="py-8 text-center text-sm text-text-muted">No jobs in this range.</p>
        )}
      </div>
      <CategoryMix rows={payload?.categories || []} />
      </div>

      <div className="vd-analytics-split">
        <div className="min-w-0">
          <h2 className="mb-2 text-sm font-semibold">By category</h2>
          <BreakdownTable rows={payload?.categories || []} />
        </div>
        <div className="min-w-0">
          <h2 className="mb-2 text-sm font-semibold">By source</h2>
          <BreakdownTable rows={payload?.sources || []} />
        </div>
      </div>

      <div className="vd-analytics-split">
        <div className="min-w-0">
          <h2 className="mb-2 text-sm font-semibold">By model</h2>
          <BreakdownTable rows={payload?.models || []} />
        </div>
        <div className="min-w-0">
          <h2 className="mb-2 text-sm font-semibold">By backend</h2>
          <BreakdownTable rows={payload?.backends || []} />
        </div>
      </div>

      <div className="min-w-0">
        <h2 className="mb-2 text-sm font-semibold">By agent</h2>
        <BreakdownTable rows={payload?.agents || []} />
      </div>

      <div className="grid items-start gap-5 xl:grid-cols-2">
        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-text-muted">
            Opened by us
          </div>
          <p className="mb-3 text-xs text-text-muted">
            Merge requests Yaver opened from a Jira or Azure Boards job. Click a
            card for the links. A later /yaver on the same MR does not count twice.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <CountCard
              label="Open"
              value={payload?.reviews?.ours?.opened ?? 0}
              to={reviewHref('ours', 'opened')}
            />
            <CountCard
              label="Merged"
              value={payload?.reviews?.ours?.merged ?? 0}
              tone="success"
              to={reviewHref('ours', 'merged')}
            />
            <CountCard
              label="Closed"
              value={payload?.reviews?.ours?.closed ?? 0}
              tone="muted"
              to={reviewHref('ours', 'closed')}
            />
            <CountCard
              label="Total"
              value={payload?.reviews?.ours?.total ?? 0}
              to={reviewHref('ours', 'all')}
            />
          </div>
        </div>
        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-text-muted">
            Contributed
          </div>
          <p className="mb-3 text-xs text-text-muted">
            Existing GitLab MRs and Azure PRs a /yaver follow-up worked on.
            Click a card for the links.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <CountCard
              label="Open"
              value={payload?.reviews?.contributed?.opened ?? 0}
              to={reviewHref('contributed', 'opened')}
            />
            <CountCard
              label="Merged"
              value={payload?.reviews?.contributed?.merged ?? 0}
              tone="success"
              to={reviewHref('contributed', 'merged')}
            />
            <CountCard
              label="Closed"
              value={payload?.reviews?.contributed?.closed ?? 0}
              tone="muted"
              to={reviewHref('contributed', 'closed')}
            />
            <CountCard
              label="Total"
              value={payload?.reviews?.contributed?.total ?? 0}
              to={reviewHref('contributed', 'all')}
            />
          </div>
        </div>
      </div>
    </section>
  )
}
