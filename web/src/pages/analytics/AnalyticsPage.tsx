import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchAnalytics } from '../../api/client'
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

const PERIODS = [
  { id: '24h', label: '24 hours' },
  { id: '7d', label: '7 days' },
  { id: '30d', label: '30 days' },
  { id: '90d', label: '90 days' },
  { id: '1y', label: '1 year' },
  { id: 'all', label: 'All' },
  { id: 'custom', label: 'Custom' },
] as const

const BUCKETS = [
  { id: 'auto', label: 'Auto' },
  { id: 'hour', label: 'Hour' },
  { id: 'day', label: 'Day' },
  { id: 'week', label: 'Week' },
  { id: 'month', label: 'Month' },
] as const

type SeriesKey = 'total' | 'completed' | 'error' | 'cancelled'

const OUTCOME_SERIES: { id: SeriesKey; label: string; color: string }[] = [
  { id: 'total', label: 'Total', color: '#ff7a45' },
  { id: 'completed', label: 'Completed', color: '#3ecf8e' },
  { id: 'error', label: 'Error', color: '#f25c54' },
  { id: 'cancelled', label: 'Cancelled', color: '#7b88a8' },
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

function FacetGroup({
  title,
  items,
  selected,
  onChange,
}: {
  title: string
  items: AnalyticsFacet[]
  selected: Set<string>
  onChange: (next: Set<string>) => void
}) {
  if (!items.length) return null
  return (
    <div>
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
              <span className="font-mono text-text-muted">{item.jobs}</span>
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
}: {
  label: string
  value: number
  tone?: 'success' | 'danger' | 'muted'
}) {
  const color =
    tone === 'success'
      ? 'text-success-text'
      : tone === 'danger'
        ? 'text-danger-text'
        : tone === 'muted'
          ? 'text-text-muted'
          : 'text-text'
  return (
    <div className="vd-card px-4 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-[0.12em] text-text-muted">
        {label}
      </div>
      <div className={`mt-1 font-mono text-2xl font-semibold ${color}`}>{value}</div>
    </div>
  )
}

function BreakdownTable({ rows }: { rows: AnalyticsNamedCount[] }) {
  if (!rows.length) {
    return <p className="px-4 py-6 text-sm text-text-muted">No jobs in this filter.</p>
  }
  return (
    <div className="vd-table-wrap">
      <table className="vd-table vd-table-compact">
        <thead>
          <tr>
            <th>Name</th>
            <th>Jobs</th>
            <th>Completed</th>
            <th>Error</th>
            <th>Cancelled</th>
            <th className="w-1/3">Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="font-medium">{row.label || row.id}</td>
              <td className="font-mono">{row.jobs}</td>
              <td className="font-mono text-success-text">{row.completed}</td>
              <td className="font-mono text-danger-text">{row.error}</td>
              <td className="font-mono text-text-muted">{row.cancelled}</td>
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
  const live = useLive()
  const [period, setPeriod] = useState<string>('30d')
  const [bucket, setBucket] = useState<string>('auto')
  const [customFrom, setCustomFrom] = useState('')
  const [customTo, setCustomTo] = useState('')
  const [status, setStatus] = useState<Set<string>>(() => new Set())
  const [category, setCategory] = useState<Set<string>>(() => new Set())
  const [source, setSource] = useState<Set<string>>(() => new Set())
  const [model, setModel] = useState<Set<string>>(() => new Set())
  const [backend, setBackend] = useState<Set<string>>(() => new Set())
  const [agent, setAgent] = useState<Set<string>>(() => new Set())
  const [repository, setRepository] = useState('')
  const [issueKey, setIssueKey] = useState('')
  const [q, setQ] = useState('')
  const [debouncedRepo, setDebouncedRepo] = useState('')
  const [debouncedKey, setDebouncedKey] = useState('')
  const [debouncedQ, setDebouncedQ] = useState('')
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
    const t = window.setTimeout(() => {
      setDebouncedRepo(repository.trim())
      setDebouncedKey(issueKey.trim())
      setDebouncedQ(q.trim())
    }, 250)
    return () => window.clearTimeout(t)
  }, [repository, issueKey, q])

  const load = useCallback(
    async (opts?: { quiet?: boolean }) => {
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac
      const req = ++reqId.current
      if (!opts?.quiet) setLoading(true)
      try {
        const data = await fetchAnalytics({
          period: period === 'custom' ? 'all' : period,
          bucket,
          from: period === 'custom' ? isoFromLocal(customFrom) : undefined,
          to: period === 'custom' ? isoFromLocal(customTo) : undefined,
          status: csv(status) || undefined,
          category: csv(category) || undefined,
          source: csv(source) || undefined,
          model: csv(model) || undefined,
          backend: csv(backend) || undefined,
          agent: csv(agent) || undefined,
          repository: debouncedRepo || undefined,
          issueKey: debouncedKey || undefined,
          q: debouncedQ || undefined,
          signal: ac.signal,
        })
        if (req !== reqId.current) return
        setPayload(data)
        setError(null)
      } catch (e) {
        if (req !== reqId.current || ac.signal.aborted) return
        if (!opts?.quiet) {
          setError(e instanceof Error ? e.message : 'Load failed')
        }
      } finally {
        if (req === reqId.current) setLoading(false)
      }
    },
    [
      period,
      bucket,
      customFrom,
      customTo,
      status,
      category,
      source,
      model,
      backend,
      agent,
      debouncedRepo,
      debouncedKey,
      debouncedQ,
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
    // bucket/period click and could abort the in-flight chart request.
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
    setRepository('')
    setIssueKey('')
    setQ('')
  }

  const filterActive =
    status.size +
      category.size +
      source.size +
      model.size +
      backend.size +
      agent.size >
      0 ||
    Boolean(debouncedRepo || debouncedKey || debouncedQ)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Workbench"
        title="Analytics"
        description="Job volume over time. Filters apply to every chart and table on this page."
        actions={
          loading ? (
            <span className="inline-flex items-center gap-2 text-xs text-text-muted">
              <Spinner /> Loading
            </span>
          ) : (
            <span className="text-xs text-text-muted">
              {payload?.matched ?? 0} of {payload?.in_range ?? 0} jobs in range
              {payload ? ` · ${payload.scanned} stored` : ''}
            </span>
          )
        }
      />

      {error && <Alert>{error}</Alert>}

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
          {PERIODS.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => setPeriod(p.id)}
              className={`rounded-full px-3 py-1 text-xs font-semibold ${
                period === p.id
                  ? 'bg-accent text-[#1a0d08]'
                  : 'text-text-secondary hover:text-text'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
          {BUCKETS.map((b) => (
            <button
              key={b.id}
              type="button"
              onClick={() => setBucket(b.id)}
              className={`rounded-full px-3 py-1 text-xs font-semibold ${
                bucket === b.id
                  ? 'bg-accent text-[#1a0d08]'
                  : 'text-text-secondary hover:text-text'
              }`}
            >
              {b.label}
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
              value={customTo || localInputFromIso(payload?.range.end || '')}
              onChange={(e) => setCustomTo(e.target.value)}
            />
          </label>
        </div>
      )}

      <div className="vd-card grid gap-4 p-4 md:grid-cols-3 lg:grid-cols-4">
        <label className="text-xs text-text-muted md:col-span-2">
          Search
          <input
            className="vd-input mt-1"
            placeholder="Key, title, model, repo"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </label>
        <label className="text-xs text-text-muted">
          Issue key
          <input
            className="vd-input mt-1 font-mono"
            placeholder="KAN-12"
            value={issueKey}
            onChange={(e) => setIssueKey(e.target.value)}
          />
        </label>
        <label className="text-xs text-text-muted">
          Repository
          <input
            className="vd-input mt-1"
            placeholder="group/app"
            value={repository}
            onChange={(e) => setRepository(e.target.value)}
          />
        </label>
        <FacetGroup title="Status" items={facets.status || []} selected={status} onChange={setStatus} />
        <FacetGroup
          title="Category"
          items={facets.category || []}
          selected={category}
          onChange={setCategory}
        />
        <FacetGroup title="Source" items={facets.source || []} selected={source} onChange={setSource} />
        <FacetGroup
          title="Backend"
          items={facets.backend || []}
          selected={backend}
          onChange={setBackend}
        />
        <FacetGroup title="Model" items={facets.model || []} selected={model} onChange={setModel} />
        <FacetGroup title="Agent" items={facets.agent || []} selected={agent} onChange={setAgent} />
        {filterActive && (
          <div className="flex items-end">
            <button type="button" className="vd-btn vd-btn-secondary" onClick={resetFilters}>
              Reset filters
            </button>
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <CountCard label="Total jobs" value={payload?.totals.jobs ?? 0} />
        <CountCard
          label="Completed"
          value={payload?.totals.completed ?? 0}
          tone="success"
        />
        <CountCard label="Error" value={payload?.totals.error ?? 0} tone="danger" />
        <CountCard
          label="Cancelled"
          value={payload?.totals.cancelled ?? 0}
          tone="muted"
        />
        <CountCard label="Models used" value={payload?.models.length ?? 0} />
      </div>

      <div className="vd-card p-4">
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
            Select Total, Completed, Error, or Cancelled to show the chart.
          </p>
        ) : outcomeSeries.some((s) => s.values.some((v) => v > 0)) ? (
          <LineChart labels={labels} series={outcomeSeries} />
        ) : (
          <p className="py-8 text-center text-sm text-text-muted">No jobs in this range.</p>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <h2 className="mb-2 text-sm font-semibold">By category</h2>
          <BreakdownTable rows={payload?.categories || []} />
        </div>
        <div>
          <h2 className="mb-2 text-sm font-semibold">By source</h2>
          <BreakdownTable rows={payload?.sources || []} />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <h2 className="mb-2 text-sm font-semibold">By model</h2>
          <BreakdownTable rows={payload?.models || []} />
        </div>
        <div>
          <h2 className="mb-2 text-sm font-semibold">By backend</h2>
          <BreakdownTable rows={payload?.backends || []} />
        </div>
      </div>
    </section>
  )
}
