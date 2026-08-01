import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelTask,
  dashboardWsUrl,
  fetchDashboard,
  fetchJobs,
  fetchModels,
  fetchTaskDetail,
  patchSettings,
  startTask,
} from './api'
import {
  navigateTo,
  parseLocation,
  pathForTab,
  pathForTask,
  tabFromRoute,
} from './routes'
import type {
  DashboardPayload,
  JobItem,
  JobsPayload,
  ModelsPayload,
  SettingsPayload,
  TaskDetail,
} from './types'

type Tab = 'tasks' | 'poll' | 'settings'

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-slate-700 text-slate-200',
  planning: 'bg-sky-900/80 text-sky-200 ring-1 ring-sky-700/50',
  plan_ready: 'bg-cyan-900/70 text-cyan-100 ring-1 ring-cyan-700/40',
  executing: 'bg-amber-900/70 text-amber-100 ring-1 ring-amber-700/40',
  completed: 'bg-emerald-900/70 text-emerald-100 ring-1 ring-emerald-700/40',
  error: 'bg-rose-900/70 text-rose-100 ring-1 ring-rose-700/40',
  cancelled: 'bg-slate-800 text-slate-400 ring-1 ring-slate-600/40',
}

function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_STYLES[status] || 'bg-slate-700 text-slate-200'
  return (
    <span className={`inline-flex rounded-md px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${cls}`}>
      {status.replace('_', ' ')}
    </span>
  )
}

function formatCountdown(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  const s = Math.max(0, seconds)
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}m ${r}s` : `${r}s`
}

export default function App() {
  const [tab, setTab] = useState<Tab>('tasks')
  const [data, setData] = useState<DashboardPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [localClock, setLocalClock] = useState(() => new Date())
  const [saving, setSaving] = useState(false)
  const [settingsDraft, setSettingsDraft] = useState<Partial<SettingsPayload>>({})
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<'overview' | 'prompts' | 'opencode' | 'logs'>(
    'overview',
  )
  const [cancelling, setCancelling] = useState(false)
  const [starting, setStarting] = useState(false)
  const [issueFilter, setIssueFilter] = useState('')
  const [jobsView, setJobsView] = useState<JobsPayload | null>(null)
  const [jobsPage, setJobsPage] = useState(1)
  const [jobsPageSize] = useState(25)
  const jobsPageRef = useRef(1)
  jobsPageRef.current = jobsPage
  /** Tab to return to when closing detail (e.g. poll → detail → back to poll). */
  const detailReturnTab = useRef<Tab>('tasks')
  /** When opening detail from a job row, highlight that job's ids */
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [settingsDirty, setSettingsDirty] = useState(false)
  /** Model inventory from GET /api/models only (not derived on the client). */
  const [modelsPayload, setModelsPayload] = useState<ModelsPayload | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsFetchError, setModelsFetchError] = useState<string | null>(null)
  const issueFilterRef = useRef(issueFilter)
  issueFilterRef.current = issueFilter
  const detailRequestId = useRef(0)
  const jobsRequestId = useRef(0)
  const lastPollCycle = useRef<number | null>(null)
  const openIssueKeyRef = useRef<string | null>(null)
  const selectedJobIdRef = useRef<string | null>(null)
  selectedJobIdRef.current = selectedJobId
  const lastDetailRefreshRef = useRef(0)

  const applyPayload = useCallback((payload: DashboardPayload) => {
    setData(payload)
    setError(null)
    // Jobs list is always loaded via paginated REST (not full WS dump)
    // Soft-refresh open task detail so Jira description/status stay live
    const openKey = openIssueKeyRef.current
    if (openKey) {
      const now = Date.now()
      if (now - lastDetailRefreshRef.current >= 4000) {
        lastDetailRefreshRef.current = now
        const jobId = selectedJobIdRef.current
        void (async () => {
          try {
            const d = await fetchTaskDetail(openKey)
            if (openIssueKeyRef.current !== openKey) return
            setDetail(d)
            if (jobId && d.jobs?.some((j) => j.job_id === jobId)) {
              setSelectedJobId(jobId)
            }
          } catch {
            /* keep existing detail on transient failure */
          }
        })()
      }
    }
  }, [])

  const reloadJobs = useCallback(
    async (opts?: { filter?: string; page?: number }) => {
      const req = ++jobsRequestId.current
      const filter = (opts?.filter ?? issueFilterRef.current).trim()
      const page = opts?.page ?? jobsPageRef.current
      try {
        const j = await fetchJobs({
          issueKey: filter || undefined,
          page,
          pageSize: jobsPageSize,
        })
        if (req !== jobsRequestId.current) return
        setJobsView(j)
      } catch (e) {
        if (req !== jobsRequestId.current) return
        setError(e instanceof Error ? e.message : 'Failed to load jobs')
      }
    },
    [jobsPageSize],
  )

  const loadDashboard = useCallback(() => {
    fetchDashboard()
      .then((payload) => {
        applyPayload(payload)
        void reloadJobs()
      })
      .catch((e: Error) => setError(e.message))
  }, [applyPayload, reloadJobs])

  const closeDetail = useCallback((opts?: { skipNavigate?: boolean }) => {
    detailRequestId.current += 1
    openIssueKeyRef.current = null
    setDetail(null)
    setDetailError(null)
    setDetailLoading(false)
    setSelectedJobId(null)
    if (!opts?.skipNavigate) {
      const back = detailReturnTab.current
      navigateTo(pathForTab(back))
      setTab(back)
    }
  }, [])

  const openTaskDetail = async (
    issueKey: string,
    jobId?: string | null,
    opts?: { skipNavigate?: boolean; replace?: boolean; fromTab?: Tab },
  ) => {
    const key = issueKey.trim().toUpperCase()
    if (!key) return
    const req = ++detailRequestId.current
    openIssueKeyRef.current = key
    if (opts?.fromTab) {
      detailReturnTab.current = opts.fromTab
    } else if (!opts?.skipNavigate) {
      // Remember current tab so Back returns to Poll / Jobs correctly
      detailReturnTab.current = tab
    }
    setDetailLoading(true)
    setDetailError(null)
    setDetailTab('overview')
    setSelectedJobId(jobId ?? null)
    if (!opts?.skipNavigate) {
      navigateTo(pathForTask(key, jobId), opts?.replace === true)
    }
    try {
      const d = await fetchTaskDetail(key)
      if (req !== detailRequestId.current) return
      setDetail(d)
      lastDetailRefreshRef.current = Date.now()
      let resolvedJob = jobId ?? null
      if (!resolvedJob && d.jobs?.length) {
        resolvedJob = d.jobs[0].job_id
        setSelectedJobId(resolvedJob)
        // Keep URL in sync once we know the newest job id
        if (!opts?.skipNavigate && resolvedJob) {
          navigateTo(pathForTask(key, resolvedJob), true)
        }
      }
    } catch (e) {
      if (req !== detailRequestId.current) return
      setDetail(null)
      setDetailError(e instanceof Error ? e.message : 'Failed to load task')
    } finally {
      if (req === detailRequestId.current) {
        setDetailLoading(false)
      }
    }
  }

  const refreshDetail = async () => {
    if (!detail?.issue_key) return
    await openTaskDetail(detail.issue_key, selectedJobId, {
      skipNavigate: true,
    })
  }

  // Deep-link / browser back-forward
  useEffect(() => {
    const applyRoute = () => {
      const route = parseLocation()
      const nextTab = tabFromRoute(route)
      setTab(nextTab)
      if (route.kind === 'task') {
        void openTaskDetail(route.issueKey, route.jobId, { skipNavigate: true })
      } else {
        closeDetail({ skipNavigate: true })
      }
    }
    applyRoute()
    window.addEventListener('popstate', applyRoute)
    return () => window.removeEventListener('popstate', applyRoute)
    // Intentionally once on mount; openTaskDetail/closeDetail are stable enough via refs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectedJob: JobItem | null = useMemo(() => {
    if (!detail?.jobs?.length) return null
    if (selectedJobId) {
      return detail.jobs.find((j) => j.job_id === selectedJobId) ?? detail.jobs[0]
    }
    return detail.jobs[0]
  }, [detail, selectedJobId])

  const onCancelJob = async () => {
    if (!detail?.issue_key || !detail.can_cancel) return
    if (!window.confirm(`Cancel running work for ${detail.issue_key}?`)) return
    setCancelling(true)
    try {
      await cancelTask(detail.issue_key)
      await refreshDetail()
      const dash = await fetchDashboard()
      applyPayload(dash)
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : 'Cancel failed')
    } finally {
      setCancelling(false)
    }
  }

  const onStartPlan = async () => {
    if (!detail?.issue_key || !detail.can_start) return
    if (!window.confirm(`Start plan execution for ${detail.issue_key}?`)) return
    setStarting(true)
    try {
      await startTask(detail.issue_key)
      await refreshDetail()
      const dash = await fetchDashboard()
      applyPayload(dash)
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : 'Start failed')
    } finally {
      setStarting(false)
    }
  }

  useEffect(() => {
    const id = window.setInterval(() => setLocalClock(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    const t = window.setTimeout(() => {
      setJobsPage(1)
      void reloadJobs({ filter: issueFilter, page: 1 })
    }, 250)
    return () => window.clearTimeout(t)
  }, [issueFilter, reloadJobs])

  useEffect(() => {
    void reloadJobs({ page: jobsPage })
  }, [jobsPage, reloadJobs])

  // Refresh jobs list on each poll cycle
  useEffect(() => {
    const cycle = data?.poll?.cycle
    if (cycle == null) return
    if (lastPollCycle.current === cycle) return
    lastPollCycle.current = cycle
    void reloadJobs()
  }, [data?.poll?.cycle, reloadJobs])

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let retry: number | undefined

    const connect = () => {
      if (closed) return
      try {
        ws = new WebSocket(dashboardWsUrl())
        ws.onopen = () => setConnected(true)
        ws.onclose = () => {
          setConnected(false)
          if (!closed) retry = window.setTimeout(connect, 2000)
        }
        ws.onerror = () => {
          setConnected(false)
        }
        ws.onmessage = (ev) => {
          try {
            applyPayload(JSON.parse(ev.data))
          } catch {
            /* ignore malformed */
          }
        }
      } catch {
        setConnected(false)
        retry = window.setTimeout(connect, 2000)
      }
    }

    loadDashboard()
    connect()
    return () => {
      closed = true
      if (retry) window.clearTimeout(retry)
      ws?.close()
    }
  }, [applyPayload, loadDashboard])

  useEffect(() => {
    if (!data?.settings || settingsDirty) return
    setSettingsDraft({
      jira_board_id: data.settings.jira_board_id,
      poll_interval_seconds: data.settings.poll_interval_seconds,
      trigger_labels: data.settings.trigger_labels,
      trigger_on_assignment: data.settings.trigger_on_assignment,
      auto_start_plans: data.settings.auto_start_plans,
      max_concurrent_jobs: data.settings.max_concurrent_jobs,
      default_model: data.settings.default_model,
    })
  }, [data?.settings, settingsDirty])

  const loadModels = useCallback(async (refresh = false) => {
    setModelsLoading(true)
    setModelsFetchError(null)
    try {
      const payload = await fetchModels(refresh)
      setModelsPayload(payload)
    } catch (e) {
      setModelsFetchError(e instanceof Error ? e.message : 'Failed to load models')
    } finally {
      setModelsLoading(false)
    }
  }, [])

  // Settings tab: load inventory from backend API (display only)
  useEffect(() => {
    if (tab !== 'settings') return
    void loadModels(false)
  }, [tab, loadModels])

  const displayTime = useMemo(() => {
    // Smooth local clock; server_time is still exposed in meta for API consumers
    return localClock.toLocaleString()
  }, [localClock])

  const countdown = data?.poll.seconds_until_next_poll
  // Soft local countdown tick between WS pushes
  const [tickLeft, setTickLeft] = useState<number | null>(null)
  useEffect(() => {
    if (countdown == null) {
      setTickLeft(null)
      return
    }
    setTickLeft(countdown)
  }, [countdown, data?.poll.next_poll_at, data?.poll.cycle])

  useEffect(() => {
    if (tickLeft == null) return
    const id = window.setInterval(() => {
      setTickLeft((v) => (v == null ? v : Math.max(0, v - 1)))
    }, 1000)
    return () => window.clearInterval(id)
  }, [tickLeft == null])

  const onSaveSettings = async () => {
    setSaving(true)
    try {
      const body = {
        jira_board_id: settingsDraft.jira_board_id,
        poll_interval_seconds: Number(settingsDraft.poll_interval_seconds),
        trigger_labels: settingsDraft.trigger_labels,
        trigger_on_assignment: settingsDraft.trigger_on_assignment,
        auto_start_plans: settingsDraft.auto_start_plans,
        max_concurrent_jobs: Number(settingsDraft.max_concurrent_jobs),
        default_model: (settingsDraft.default_model ?? '').trim(),
      }
      if (
        !Number.isFinite(body.poll_interval_seconds) ||
        !Number.isFinite(body.max_concurrent_jobs)
      ) {
        throw new Error('Poll interval and max concurrent jobs must be numbers')
      }
      const updated = await patchSettings(body)
      setSettingsDirty(false)
      setData((prev) =>
        prev
          ? {
              ...prev,
              settings: updated,
            }
          : prev,
      )
      // Keep models panel default_model in sync after save
      setModelsPayload((prev) =>
        prev
          ? { ...prev, default_model: updated.default_model }
          : prev,
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Settings save failed')
    } finally {
      setSaving(false)
    }
  }

  const navBtn = (id: Tab, label: string) => (
    <button
      key={id}
      type="button"
      onClick={() => {
        navigateTo(pathForTab(id))
        setTab(id)
        closeDetail({ skipNavigate: true })
      }}
      className={`rounded-lg px-3 py-2 text-sm font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400 ${
        tab === id && !detail && !detailLoading
          ? 'bg-indigo-600 text-white shadow'
          : 'text-slate-300 hover:bg-slate-800 hover:text-white'
      }`}
    >
      {label}
    </button>
  )

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-950 via-slate-950 to-slate-900">
      <header className="border-b border-slate-800/80 bg-slate-950/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-4">
          <div>
            <h1 className="text-lg font-semibold tracking-tight text-slate-50">
              JIRA Virtual Developer
            </h1>
            <p className="text-xs text-slate-400">Operations dashboard</p>
          </div>
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-1.5">
              <span className="text-slate-500">Version </span>
              <span className="font-mono text-slate-200">{data?.meta.version ?? '—'}</span>
            </div>
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-1.5 font-mono text-slate-200">
              {displayTime}
            </div>
            <div
              className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium ${
                connected
                  ? 'border-emerald-800/60 bg-emerald-950/40 text-emerald-300'
                  : 'border-amber-800/60 bg-amber-950/40 text-amber-200'
              }`}
            >
              <span
                className={`h-2 w-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-amber-400'}`}
              />
              {connected ? 'Live' : 'Reconnecting'}
            </div>
          </div>
        </div>
        <div className="mx-auto flex max-w-7xl gap-2 px-4 pb-3">
          {navBtn('tasks', 'Jobs')}
          {navBtn('poll', 'Poll monitor')}
          {navBtn('settings', 'Settings')}
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6">
        {error && (
          <div
            role="alert"
            className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-rose-800/50 bg-rose-950/40 px-4 py-3 text-sm text-rose-100"
          >
            <span>{error}</span>
            <button
              type="button"
              onClick={() => {
                setError(null)
                loadDashboard()
              }}
              className="rounded-md bg-rose-900/60 px-3 py-1 text-xs font-medium text-rose-50 hover:bg-rose-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-rose-300"
            >
              Retry
            </button>
          </div>
        )}

        {!data && !error && (
          <div className="text-sm text-slate-400" aria-busy="true">
            Loading dashboard…
          </div>
        )}

        {/* Full-page task detail (replaces list while open) */}
        {(detail || detailLoading || detailError) && (
          <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  onClick={() => {
                    closeDetail()
                  }}
                  className="mb-2 text-sm text-indigo-400 hover:text-indigo-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400"
                >
                  ← Back to{' '}
                  {detailReturnTab.current === 'poll'
                    ? 'poll monitor'
                    : detailReturnTab.current === 'settings'
                      ? 'settings'
                      : 'jobs'}
                </button>
                <h2 className="font-mono text-xl text-indigo-300">
                  {detail?.issue_key ?? (detailLoading ? 'Loading…' : '—')}
                </h2>
                <p className="text-base text-slate-200">
                  {selectedJob?.summary || detail?.summary}
                </p>
                {selectedJob?.description?.trim() ? (
                  <p className="mt-2 whitespace-pre-wrap rounded-lg border border-indigo-900/50 bg-indigo-950/30 px-3 py-2 text-sm text-indigo-100">
                    <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-indigo-300/80">
                      Description for job {selectedJob.job_id}
                    </span>
                    {selectedJob.description}
                  </p>
                ) : null}
                {selectedJob?.summary &&
                  detail?.summary &&
                  selectedJob.summary !== detail.summary && (
                    <p className="mt-0.5 text-xs text-slate-500">
                      Current issue summary: {detail.summary}
                    </p>
                  )}
                {detail && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <StatusBadge status={detail.status} />
                    {detail.jira_status && (
                      <span
                        className="rounded-md border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[11px] text-slate-300"
                        title="Jira board column (live)"
                      >
                        Jira: {detail.jira_status}
                      </span>
                    )}
                    {detail.live && (
                      <span className="text-[10px] font-semibold uppercase text-amber-300">
                        agent live
                      </span>
                    )}
                    {detail.jira_live === false && (
                      <span className="text-[10px] text-amber-400/90">
                        Jira live fetch unavailable — showing local cache
                      </span>
                    )}
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {detail?.can_start && (
                  <button
                    type="button"
                    disabled={starting}
                    onClick={onStartPlan}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                  >
                    {starting ? 'Starting…' : 'Start plan'}
                  </button>
                )}
                {detail?.can_cancel && (
                  <button
                    type="button"
                    disabled={cancelling}
                    onClick={onCancelJob}
                    className="rounded-lg bg-rose-700 px-4 py-2 text-sm font-medium text-white hover:bg-rose-600 disabled:opacity-50"
                  >
                    {cancelling ? 'Cancelling…' : 'Cancel job'}
                  </button>
                )}
                <button
                  type="button"
                  onClick={refreshDetail}
                  className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800"
                >
                  Refresh
                </button>
              </div>
            </div>

            <div className="flex flex-wrap gap-1 border-b border-slate-800">
              {(
                [
                  ['overview', 'Overview'],
                  ['prompts', 'Prompts'],
                  ['opencode', 'OpenCode output'],
                  ['logs', 'System logs'],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setDetailTab(id)}
                  className={`rounded-t-lg px-4 py-2.5 text-sm font-medium ${
                    detailTab === id
                      ? 'border border-b-0 border-slate-700 bg-slate-900 text-indigo-300'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            <div className="min-h-[50vh] rounded-xl border border-slate-800 bg-slate-900/40 p-5">
              {detailLoading && (
                <p className="text-sm text-slate-400">Loading task detail…</p>
              )}
              {detailError && (
                <p className="text-sm text-rose-300">{detailError}</p>
              )}

              {detail && detailTab === 'overview' && (
                <div className="space-y-4 text-sm">
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-4">
                      <div className="text-xs text-slate-500">Workflow / agent</div>
                      <div className="mt-1 text-slate-200">
                        {detail.workflow_type ?? '—'} / {detail.prompts?.agent ?? '—'}
                      </div>
                    </div>
                    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-4">
                      <div className="text-xs text-slate-500">Progress</div>
                      <div className="mt-1 text-slate-200">{detail.progress_percentage}%</div>
                    </div>
                    <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-4">
                      <div className="text-xs text-slate-500">Started</div>
                      <div className="mt-1 font-mono text-xs text-slate-300">
                        {detail.started_at ?? '—'}
                      </div>
                    </div>
                    <div className="rounded-lg border border-indigo-900/40 bg-indigo-950/20 p-4 sm:col-span-2 lg:col-span-3">
                      <div className="text-xs font-medium uppercase tracking-wide text-indigo-300/80">
                        Selected job (ids for this run only)
                      </div>
                      {selectedJob ? (
                        <div className="mt-2 grid gap-2 sm:grid-cols-2">
                          <div>
                            <div className="text-[10px] uppercase text-slate-500">Job id</div>
                            <div className="break-all font-mono text-xs text-slate-200">
                              {selectedJob.job_id}
                            </div>
                          </div>
                          <div>
                            <div className="text-[10px] uppercase text-slate-500">Status</div>
                            <StatusBadge status={selectedJob.status} />
                          </div>
                          <div>
                            <div className="text-[10px] uppercase text-slate-500">
                              Task id (this job)
                            </div>
                            <div className="break-all font-mono text-sm text-amber-200">
                              {selectedJob.task_id ?? '—'}
                            </div>
                            {(selectedJob.task_ids?.length ?? 0) > 1 && (
                              <div className="mt-1 font-mono text-[10px] text-slate-500">
                                retries: {selectedJob.task_ids!.join(', ')}
                              </div>
                            )}
                          </div>
                          <div>
                            <div className="text-[10px] uppercase text-slate-500">
                              OpenCode session (this job)
                            </div>
                            <div className="break-all font-mono text-sm text-cyan-300">
                              {selectedJob.opencode_session_id ?? '—'}
                            </div>
                          </div>
                          <div className="sm:col-span-2">
                            <div className="text-[10px] uppercase text-slate-500">
                              Session log
                            </div>
                            <div className="break-all font-mono text-[11px] text-slate-400">
                              {selectedJob.session_log_path ?? '—'}
                            </div>
                          </div>
                          <div className="sm:col-span-2">
                            <div className="text-[10px] uppercase text-slate-500">
                              Summary at job start
                            </div>
                            <div className="text-sm text-slate-200">
                              {selectedJob.summary || '—'}
                            </div>
                          </div>
                        </div>
                      ) : (
                        <p className="mt-2 text-sm text-slate-500">
                          No job selected. Pick a row in the jobs table below.
                        </p>
                      )}
                      <div className="mt-4 border-t border-slate-800 pt-3">
                        <div className="text-[10px] uppercase tracking-wide text-slate-500">
                          Latest on issue (live slot — overwrites each new run)
                        </div>
                        <div className="mt-1 break-all font-mono text-xs text-slate-400">
                          task: {detail.current_task_id ?? '—'}
                          <br />
                          session: {detail.current_opencode_session_id ?? '—'}
                        </div>
                        {(detail.task_ids?.length ?? 0) > 0 && (
                          <div className="mt-2">
                            <div className="text-[10px] uppercase text-slate-500">
                              All task ids (history)
                            </div>
                            <ul className="mt-0.5 space-y-0.5 font-mono text-[11px] text-slate-400">
                              {detail.task_ids!.map((tid) => (
                                <li key={tid}>{tid}</li>
                              ))}
                            </ul>
                          </div>
                        )}
                        {(detail.opencode_session_ids?.length ?? 0) > 0 && (
                          <div className="mt-2">
                            <div className="text-[10px] uppercase text-slate-500">
                              All OpenCode sessions (history)
                            </div>
                            <ul className="mt-0.5 space-y-0.5 font-mono text-[11px] text-slate-400">
                              {detail.opencode_session_ids!.map((sid) => (
                                <li key={sid} className="break-all">
                                  {sid}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  {detail.error_message && (
                    <div>
                      <div className="mb-1 text-xs uppercase tracking-wide text-rose-400">
                        Error
                      </div>
                      <pre className="whitespace-pre-wrap rounded-lg border border-rose-900/40 bg-rose-950/30 p-4 text-xs text-rose-100">
                        {detail.error_message}
                      </pre>
                    </div>
                  )}
                  {detail.retry_history?.length > 0 && (
                    <div>
                      <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">
                        Retry history
                      </div>
                      <pre className="max-h-56 overflow-auto rounded-lg border border-slate-800 bg-slate-950/60 p-4 text-xs text-slate-400">
                        {JSON.stringify(detail.retry_history, null, 2)}
                      </pre>
                    </div>
                  )}
                  <div>
                    <div className="mb-2 text-xs uppercase tracking-wide text-slate-500">
                      All jobs for {detail.issue_key} ({detail.jobs?.length ?? 0}) — click a
                      row to inspect that run&apos;s task/session ids
                    </div>
                    <JobsTable
                      jobs={detail.jobs ?? []}
                      selectedJobId={selectedJobId}
                      onOpenIssue={(key, jobId) => {
                        setSelectedJobId(jobId ?? null)
                        if (key !== detail.issue_key) {
                          void openTaskDetail(key, jobId)
                        }
                      }}
                      compact
                    />
                  </div>
                </div>
              )}

              {detail && detailTab === 'prompts' && (
                <div className="space-y-4 text-sm">
                  <p className="text-xs text-slate-500">
                    Exact text sent to the agent for each run (
                    <code className="text-slate-400">agent/AGENT_PROMPT.md</code> + Jira body,
                    frozen in <code className="text-slate-400">*.prompt.txt</code>).
                  </p>
                  {(() => {
                    const captured = detail.prompts?.captured_prompt_files || []
                    const selectedPath = selectedJob?.prompt_path
                    const jobPrompt =
                      selectedPath &&
                      captured.find(
                        (f) =>
                          f.path === selectedPath ||
                          f.path?.endsWith(selectedPath.split(/[/\\]/).pop() || ''),
                      )
                    const others = captured.filter((f) => f !== jobPrompt)
                    if (captured.length === 0) {
                      return (
                        <p className="text-slate-500">
                          No captured prompt yet for this issue (written when an agent run
                          starts).
                        </p>
                      )
                    }
                    return (
                      <>
                        {jobPrompt && (
                          <PromptBlock
                            title={`Sent to agent · ${selectedJob?.job_id || 'selected job'}${
                              jobPrompt.truncated ? ' (truncated)' : ''
                            }`}
                            body={jobPrompt.content || jobPrompt.error || '(empty)'}
                          />
                        )}
                        {!jobPrompt &&
                          captured.map((f) => (
                            <PromptBlock
                              key={f.path}
                              title={`Sent to agent · ${f.name || f.path}${
                                f.truncated ? ' (truncated)' : ''
                              }`}
                              body={f.content || f.error || '(empty)'}
                            />
                          ))}
                        {jobPrompt && others.length > 0 && (
                          <>
                            <div className="text-xs uppercase tracking-wide text-slate-500">
                              Other runs
                            </div>
                            {others.map((f) => (
                              <PromptBlock
                                key={f.path}
                                title={`${f.name || f.path}${f.truncated ? ' (truncated)' : ''}`}
                                body={f.content || f.error || '(empty)'}
                              />
                            ))}
                          </>
                        )}
                      </>
                    )
                  })()}
                </div>
              )}

              {detail && detailTab === 'opencode' && (
                <div className="space-y-4 text-sm">
                  {detail.session_logs.length === 0 && (
                    <p className="text-slate-500">
                      No session logs under .jira-agent/sessions for this issue yet.
                    </p>
                  )}
                  {selectedJob?.session_log_path && (
                    <p className="text-xs text-slate-500">
                      Selected job log: {selectedJob.session_log_path}
                    </p>
                  )}
                  {detail.session_logs.map((f) => {
                    const isSelected =
                      !!selectedJob?.session_log_path &&
                      (f.path === selectedJob.session_log_path ||
                        f.path?.endsWith(
                          selectedJob.session_log_path!.split(/[/\\]/).pop() || '',
                        ))
                    return (
                      <PromptBlock
                        key={f.path}
                        title={`${f.name || f.path}${f.truncated ? ' (truncated)' : ''}${
                          isSelected ? ' · SELECTED JOB' : ''
                        }`}
                        body={f.content || f.error || '(empty)'}
                        mono
                      />
                    )
                  })}
                </div>
              )}

              {detail && detailTab === 'logs' && (
                <div className="space-y-2 text-sm">
                  <p className="text-xs text-slate-500">
                    In-process log lines that mention this issue key (since daemon start).
                  </p>
                  {detail.system_logs.length === 0 && (
                    <p className="text-slate-500">No matching system log lines.</p>
                  )}
                  <div className="max-h-[70vh] overflow-auto rounded-lg border border-slate-800 bg-slate-950/80 p-4 font-mono text-[11px] leading-relaxed text-slate-300">
                    {detail.system_logs.map((line, i) => (
                      <div
                        key={`${line.timestamp}-${i}`}
                        className="border-b border-slate-800/50 py-0.5"
                      >
                        <span className="text-slate-500">{line.timestamp} </span>
                        {line.message}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </section>
        )}

        {data && tab === 'tasks' && !detail && !detailLoading && !detailError && (
          <section className="space-y-6">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-base font-semibold text-slate-100">Jobs</h2>
                <p className="text-xs text-slate-500">
                  Each agent run is a job. Filter by Jira key, open a row for detail.
                  Board issues live under Poll monitor — click a key there for the same detail view.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <label className="text-xs text-slate-400">
                  Filter by Jira issue
                  <input
                    className="ml-2 w-40 rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-indigo-500"
                    placeholder="e.g. KAN-1"
                    value={issueFilter}
                    onChange={(e) => setIssueFilter(e.target.value)}
                  />
                </label>
                {issueFilter.trim() && (
                  <button
                    type="button"
                    className="text-xs text-indigo-400 hover:underline"
                    onClick={() => setIssueFilter('')}
                  >
                    Clear
                  </button>
                )}
              </div>
            </div>

            {(() => {
              const total = jobsView?.total ?? 0
              const page = jobsView?.page ?? jobsPage
              const size = jobsView?.page_size ?? jobsPageSize
              const totalPages = Math.max(1, Math.ceil(total / size) || 1)
              const from = total === 0 ? 0 : (page - 1) * size + 1
              const to = Math.min(page * size, total)
              return (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500">
                    <span>
                      Showing {from}–{to} of {total} job(s)
                      {issueFilter.trim()
                        ? ` for ${issueFilter.trim().toUpperCase()}`
                        : ''}
                    </span>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        disabled={page <= 1}
                        onClick={() => setJobsPage((p) => Math.max(1, p - 1))}
                        className="rounded-md border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Previous
                      </button>
                      <span className="font-mono text-slate-400">
                        Page {page} / {totalPages}
                      </span>
                      <button
                        type="button"
                        disabled={page >= totalPages}
                        onClick={() => setJobsPage((p) => p + 1)}
                        className="rounded-md border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Next
                      </button>
                    </div>
                  </div>

                  <JobsTable
                    jobs={jobsView?.jobs ?? []}
                    onOpenIssue={(key, jobId) => void openTaskDetail(key, jobId)}
                  />
                </>
              )
            })()}
          </section>
        )}

        {data && tab === 'poll' && !detail && !detailLoading && !detailError && (
          <section className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
                <div className="text-xs uppercase tracking-wide text-slate-500">Phase</div>
                <div className="mt-1 text-lg font-semibold capitalize text-slate-100">
                  {data.poll.phase}
                </div>
              </div>
              <div className="rounded-xl border border-indigo-900/40 bg-indigo-950/30 p-4">
                <div className="text-xs uppercase tracking-wide text-indigo-300/80">
                  Next poll in
                </div>
                <div className="mt-1 font-mono text-2xl font-semibold text-indigo-100">
                  {formatCountdown(tickLeft ?? data.poll.seconds_until_next_poll)}
                </div>
                <div className="mt-1 text-xs text-slate-500">
                  Interval {data.poll.poll_interval_seconds}s
                </div>
              </div>
              <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
                <div className="text-xs uppercase tracking-wide text-slate-500">Matched filter</div>
                <div className="mt-1 text-lg font-semibold text-slate-100">
                  {data.poll.matched_count}
                </div>
                <div className="text-xs text-slate-500">
                  Will process: {data.poll.will_process_count}
                </div>
              </div>
              <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
                <div className="text-xs uppercase tracking-wide text-slate-500">Source</div>
                <div className="mt-1 truncate text-sm font-medium text-slate-100">
                  {data.poll.source ?? '—'}
                </div>
                <div className="text-xs text-slate-500">Board {data.poll.board_id ?? '—'}</div>
              </div>
            </div>

            {data.poll.error && (
              <div className="rounded-lg border border-rose-800/50 bg-rose-950/30 px-4 py-2 text-sm text-rose-200">
                Poll error: {data.poll.error}
              </div>
            )}

            <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 shadow-xl shadow-black/20">
              <table className="min-w-full text-left text-sm">
                <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-4 py-3 font-medium">Issue</th>
                    <th className="px-4 py-3 font-medium">Jira status</th>
                    <th className="px-4 py-3 font-medium">Assignee</th>
                    <th className="px-4 py-3 font-medium">Filter</th>
                    <th className="px-4 py-3 font-medium">Local</th>
                    <th className="px-4 py-3 font-medium">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {data.poll.issues.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-8 text-center text-slate-500">
                        No bot-eligible issues this cycle (trigger label or bot
                        assignee). Unmatched board issues are hidden.
                      </td>
                    </tr>
                  )}
                  {data.poll.issues.map((i) => (
                    <tr
                      key={i.key}
                      role="button"
                      tabIndex={0}
                      className={
                        (i.will_process
                          ? 'bg-indigo-950/20 hover:bg-indigo-950/30'
                          : 'hover:bg-slate-800/40') + ' cursor-pointer'
                      }
                      onClick={() =>
                        void openTaskDetail(i.key, null, { fromTab: 'poll' })
                      }
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          void openTaskDetail(i.key, null, { fromTab: 'poll' })
                        }
                      }}
                    >
                      <td className="px-4 py-3">
                        <div className="font-mono text-indigo-300 underline-offset-2 group-hover:underline">
                          {i.key}
                        </div>
                        <div className="max-w-sm truncate text-slate-300">{i.summary}</div>
                        {i.labels.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {i.labels.map((l) => (
                              <span
                                key={l}
                                className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400"
                              >
                                {l}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-slate-300">{i.jira_status || '—'}</td>
                      <td className="px-4 py-3 text-slate-300">{i.assignee || '—'}</td>
                      <td className="px-4 py-3">
                        <div className="flex flex-col gap-1 text-xs">
                          <span className={i.matched_label ? 'text-emerald-300' : 'text-slate-500'}>
                            Label {i.matched_label ? 'match' : 'no'}
                            {i.matched_labels.length > 0 && ` (${i.matched_labels.join(', ')})`}
                          </span>
                          <span
                            className={i.matched_assignee ? 'text-emerald-300' : 'text-slate-500'}
                          >
                            Assignee {i.matched_assignee ? 'bot' : 'no'}
                          </span>
                          <span className={i.is_todo ? 'text-sky-300' : 'text-slate-500'}>
                            To Do {i.is_todo ? 'yes' : 'no'}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        {i.local_status ? (
                          <StatusBadge status={i.local_status} />
                        ) : (
                          <span className="text-slate-500">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {i.will_process ? (
                          <span className="rounded-md bg-indigo-600/30 px-2 py-1 text-xs font-medium text-indigo-200 ring-1 ring-indigo-500/40">
                            Queued
                          </span>
                        ) : (
                          <span className="text-xs text-slate-500">Skip</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-slate-500">
              Last poll: {data.poll.last_poll_at ?? '—'} · Cycle {data.poll.cycle} ·
              Showing only bot-eligible issues (label / assignee match)
            </p>
          </section>
        )}

        {data && tab === 'settings' && !detail && !detailLoading && !detailError && (
          <section className="max-w-xl space-y-4">
            <div>
              <h2 className="text-base font-semibold text-slate-100">Settings</h2>
              <p className="text-xs text-slate-500">
                Runtime values only. Secrets are never shown. Changes apply until process restart
                (unless also set in .env).
              </p>
            </div>

            <div className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/40 p-5">
              {/* OpenCode model — list from GET /api/models; FE only renders DTOs */}
              <div className="space-y-3 rounded-lg border border-indigo-700/50 bg-indigo-950/30 p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="text-sm font-semibold text-indigo-100">
                      OpenCode model
                    </div>
                    <p className="mt-1 text-xs text-slate-400">
                      List loaded from <code className="text-slate-300">GET /api/models</code>{' '}
                      (backend runs <code className="text-slate-300">opencode models</code> and
                      reads opencode.json). Saving updates runtime{' '}
                      <code className="text-slate-300">DEFAULT_MODEL</code>.
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={modelsLoading}
                    onClick={() => void loadModels(true)}
                    className="shrink-0 rounded-lg border border-indigo-600/50 px-2.5 py-1 text-xs text-indigo-200 hover:bg-indigo-900/50 disabled:opacity-50"
                  >
                    {modelsLoading ? 'Loading…' : 'Refresh list'}
                  </button>
                </div>
                <label className="block text-sm">
                  <span className="text-slate-300">Default model</span>
                  <select
                    className="mt-1 w-full rounded-lg border border-indigo-600/40 bg-slate-950 px-3 py-2 text-slate-100 outline-none focus:border-indigo-400"
                    disabled={modelsLoading && !modelsPayload}
                    value={
                      settingsDraft.default_model ??
                      data.settings.default_model ??
                      ''
                    }
                    onChange={(e) => {
                      setSettingsDirty(true)
                      setSettingsDraft((s) => ({
                        ...s,
                        default_model: e.target.value,
                      }))
                    }}
                  >
                    <option value="">
                      {modelsLoading ? 'Loading models…' : '— select a model —'}
                    </option>
                    {(modelsPayload?.models ?? []).map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label || m.id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm">
                  <span className="text-slate-300">Or type provider/model id</span>
                  <input
                    className="mt-1 w-full rounded-lg border border-indigo-600/40 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100 outline-none focus:border-indigo-400"
                    value={
                      settingsDraft.default_model ?? data.settings.default_model ?? ''
                    }
                    placeholder="opencode/deepseek-v4-flash-free"
                    onChange={(e) => {
                      setSettingsDirty(true)
                      setSettingsDraft((s) => ({
                        ...s,
                        default_model: e.target.value,
                      }))
                    }}
                  />
                </label>
                {(modelsFetchError || modelsPayload?.error) && (
                  <p className="text-xs text-amber-300">
                    {modelsFetchError || modelsPayload?.error}
                  </p>
                )}
                <div className="text-xs text-slate-400">
                  Active:{' '}
                  <span className="font-mono text-indigo-200">
                    {settingsDraft.default_model ||
                      data.settings.default_model ||
                      modelsPayload?.default_model ||
                      '(unset)'}
                  </span>
                  {modelsPayload != null && (
                    <span> · {modelsPayload.models.length} from API</span>
                  )}
                </div>
                <div className="text-xs text-slate-500 break-all">
                  {!modelsPayload ? (
                    <>Model inventory loads from GET /api/models when this tab opens.</>
                  ) : modelsPayload.opencode_config_path ? (
                    <>
                      OpenCode config:{' '}
                      <span className="font-mono text-slate-400">
                        {modelsPayload.opencode_config_path}
                      </span>
                      {modelsPayload.opencode_config_model
                        ? ` · model key: ${modelsPayload.opencode_config_model}`
                        : ''}
                    </>
                  ) : (
                    <>
                      No opencode.json / opencode.jsonc found (checked project root and
                      ~/.config/opencode). Custom provider models will appear here once you
                      add that file.
                    </>
                  )}
                </div>
              </div>

              <label className="block text-sm">
                <span className="text-slate-400">Jira host (read-only)</span>
                <input
                  disabled
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-400"
                  value={data.settings.jira_host}
                />
              </label>
              <label className="block text-sm">
                <span className="text-slate-400">Board ID</span>
                <input
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100 outline-none focus:border-indigo-500"
                  value={settingsDraft.jira_board_id ?? ''}
                  onChange={(e) => {
                    setSettingsDirty(true)
                    setSettingsDraft((s) => ({ ...s, jira_board_id: e.target.value }))
                  }}
                />
              </label>
              <label className="block text-sm">
                <span className="text-slate-400">Poll interval (seconds)</span>
                <input
                  type="number"
                  min={5}
                  max={3600}
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100 outline-none focus:border-indigo-500"
                  value={settingsDraft.poll_interval_seconds ?? 30}
                  onChange={(e) => {
                    setSettingsDirty(true)
                    setSettingsDraft((s) => ({
                      ...s,
                      poll_interval_seconds: Number(e.target.value),
                    }))
                  }}
                />
              </label>
              <label className="block text-sm">
                <span className="text-slate-400">Trigger labels (comma-separated)</span>
                <input
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100 outline-none focus:border-indigo-500"
                  value={settingsDraft.trigger_labels ?? ''}
                  onChange={(e) => {
                    setSettingsDirty(true)
                    setSettingsDraft((s) => ({ ...s, trigger_labels: e.target.value }))
                  }}
                />
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={Boolean(settingsDraft.trigger_on_assignment)}
                  onChange={(e) => {
                    setSettingsDirty(true)
                    setSettingsDraft((s) => ({
                      ...s,
                      trigger_on_assignment: e.target.checked,
                    }))
                  }}
                />
                Trigger on bot assignment
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={Boolean(settingsDraft.auto_start_plans)}
                  onChange={(e) => {
                    setSettingsDirty(true)
                    setSettingsDraft((s) => ({ ...s, auto_start_plans: e.target.checked }))
                  }}
                />
                Auto-start plans
              </label>
              <label className="block text-sm">
                <span className="text-slate-400">Max concurrent jobs</span>
                <input
                  type="number"
                  min={1}
                  max={32}
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100 outline-none focus:border-indigo-500"
                  value={settingsDraft.max_concurrent_jobs ?? 3}
                  onChange={(e) => {
                    setSettingsDirty(true)
                    setSettingsDraft((s) => ({
                      ...s,
                      max_concurrent_jobs: Number(e.target.value),
                    }))
                  }}
                />
              </label>

              <div className="grid grid-cols-2 gap-2 border-t border-slate-800 pt-3 text-xs text-slate-500">
                <div>
                  Jira token:{' '}
                  {data.settings.jira_token_configured ? 'configured' : 'missing'}
                </div>
                <div>
                  GitLab PAT:{' '}
                  {data.settings.gitlab_pat_configured ? 'configured' : 'missing'}
                </div>
                <div>Base branch: {data.settings.default_branch}</div>
                <div>
                  Dashboard: {data.settings.dashboard_host}:{data.settings.dashboard_port}
                </div>
              </div>

              <button
                type="button"
                disabled={saving}
                onClick={onSaveSettings}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save settings'}
              </button>
            </div>
          </section>
        )}
      </main>
    </div>
  )
}

function PromptBlock({
  title,
  body,
  mono = true,
}: {
  title: string
  body: string
  mono?: boolean
}) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
        {title}
      </div>
      <pre
        className={`max-h-[min(70vh,40rem)] overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950/80 p-4 text-xs leading-relaxed text-slate-200 ${
          mono ? 'font-mono' : ''
        }`}
      >
        {body}
      </pre>
    </div>
  )
}

function JobsTable({
  jobs,
  onOpenIssue,
  compact = false,
  selectedJobId = null,
}: {
  jobs: JobItem[]
  onOpenIssue: (issueKey: string, jobId?: string) => void
  compact?: boolean
  selectedJobId?: string | null
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-800 bg-slate-900/40 shadow-xl shadow-black/20">
      <table className="min-w-full text-left text-sm">
        <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-4 py-3 font-medium">Job</th>
            <th className="px-4 py-3 font-medium">Issue</th>
            <th className="px-4 py-3 font-medium">Status</th>
            <th className="px-4 py-3 font-medium">Task id</th>
            {!compact && <th className="px-4 py-3 font-medium">Workflow</th>}
            <th className="px-4 py-3 font-medium">Started</th>
            {!compact && <th className="px-4 py-3 font-medium">Progress</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/80">
          {jobs.length === 0 && (
            <tr>
              <td
                colSpan={compact ? 5 : 7}
                className="px-4 py-8 text-center text-slate-500"
              >
                No jobs for this filter. Historical session logs and new runs will appear here.
              </td>
            </tr>
          )}
          {jobs.map((j) => (
            <tr
              key={j.job_id}
              className={`hover:bg-slate-800/40 ${
                selectedJobId === j.job_id ? 'bg-indigo-950/40 ring-1 ring-inset ring-indigo-700/40' : ''
              }`}
            >
              <td className="px-4 py-3">
                <button
                  type="button"
                  className="font-mono text-[11px] text-slate-300 hover:text-indigo-300 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400"
                  title={j.job_id}
                  onClick={() => onOpenIssue(j.issue_key, j.job_id)}
                >
                  {j.job_id}
                </button>
                {j.live && (
                  <span className="ml-1 text-[10px] font-semibold uppercase text-amber-300">
                    live
                  </span>
                )}
              </td>
              <td className="px-4 py-3">
                <button
                  type="button"
                  className="font-mono text-indigo-300 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400"
                  onClick={() => onOpenIssue(j.issue_key, j.job_id)}
                >
                  {j.issue_key}
                </button>
                <div className="max-w-xs truncate text-xs text-slate-400" title={j.summary}>
                  {j.summary}
                </div>
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={j.status} />
                {j.error_message && (
                  <div className="mt-1 max-w-xs truncate text-xs text-rose-300/90">
                    {j.error_message}
                  </div>
                )}
              </td>
              <td className="px-4 py-3">
                <div
                  className="max-w-[9rem] truncate font-mono text-[11px] text-amber-200/90"
                  title={j.task_id || undefined}
                >
                  {j.task_id || '—'}
                </div>
              </td>
              {!compact && (
                <td className="px-4 py-3 text-slate-300">
                  {j.workflow_type}
                  {j.agent ? (
                    <div className="text-[10px] text-slate-500">{j.agent}</div>
                  ) : null}
                </td>
              )}
              <td className="px-4 py-3 font-mono text-[11px] text-slate-400">
                {j.started_at ?? '—'}
              </td>
              {!compact && (
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-800">
                      <div
                        className="h-full rounded-full bg-indigo-500"
                        style={{ width: `${Math.min(100, j.progress_percentage)}%` }}
                      />
                    </div>
                    <span className="font-mono text-xs text-slate-400">
                      {j.progress_percentage}%
                    </span>
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
