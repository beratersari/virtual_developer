import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelSchedule,
  cancelTask,
  createSchedule,
  dashboardWsUrl,
  deleteJob,
  deleteJobs,
  fetchDashboard,
  fetchJobById,
  fetchJobs,
  fetchModels,
  fetchSchedules,
  fetchTaskDetail,
  patchSettings,
} from './api'
import { JobDetail } from './components/JobDetail'
import { JobsPage } from './components/JobsPage'
import { PollMonitor } from './components/PollMonitor'
import { ScheduledPage } from './components/ScheduledPage'
import { SettingsPage, type SettingsDraft } from './components/SettingsPage'
import { TaskDetailPage } from './components/TaskDetailPage'
import type { JobStatusFilter } from './util/status'
import {
  navigateTo,
  parseLocation,
  pathForJob,
  pathForTab,
  pathForTask,
  tabFromRoute,
} from './routes'
import type {
  DashboardPayload,
  JobItem,
  JobsPayload,
  ModelsPayload,
  ScheduleCreateBody,
  ScheduleItem,
  SystemLogLine,
  TaskDetail,
  TextArtifact,
} from './types'

type Tab = 'tasks' | 'poll' | 'settings' | 'scheduled'
type ViewMode = 'list' | 'job' | 'task'
type JobTab = 'overview' | 'prompt' | 'opencode' | 'logs'
type TaskTab = 'overview' | 'logs'

function asJobItem(raw: Record<string, unknown> | JobItem): JobItem {
  const j = raw as JobItem
  return {
    job_id: String(j.job_id || ''),
    issue_key: String(j.issue_key || ''),
    summary: j.summary || '',
    description: j.description || '',
    workflow_type: j.workflow_type || 'execution',
    agent: j.agent || '',
    status: j.status || 'unknown',
    task_id: j.task_id ?? null,
    task_ids: j.task_ids || (j.task_id ? [j.task_id] : []),
    opencode_session_id: j.opencode_session_id ?? null,
    opencode_session_ids: j.opencode_session_ids || [],
    session_log_path: j.session_log_path ?? null,
    prompt_path: j.prompt_path ?? null,
    progress_percentage: Number(j.progress_percentage || 0),
    error_message: j.error_message ?? null,
    started_at: j.started_at ?? null,
    completed_at: j.completed_at ?? null,
    updated_at: j.updated_at ?? null,
    live: Boolean(j.live),
    feature_branch: j.feature_branch ?? null,
    merge_request_url: j.merge_request_url ?? null,
    commit_sha: j.commit_sha ?? null,
    commit_subject: j.commit_subject ?? null,
    commit_url: j.commit_url ?? null,
    delivery_status: j.delivery_status ?? null,
    delivery_note: j.delivery_note ?? null,
  }
}

export default function App() {
  const [tab, setTab] = useState<Tab>('tasks')
  const [viewMode, setViewMode] = useState<ViewMode>('list')
  const [data, setData] = useState<DashboardPayload | null>(null)
  const [dashboardError, setDashboardError] = useState<string | null>(null)
  const [jobsError, setJobsError] = useState<string | null>(null)
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [localClock, setLocalClock] = useState(() => new Date())
  const [saving, setSaving] = useState(false)
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft>({})

  // Shared detail load state
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailStale, setDetailStale] = useState(false)

  // Job view
  const [jobView, setJobView] = useState<JobItem | null>(null)
  const [jobArtifacts, setJobArtifacts] = useState<{
    prompts: TextArtifact[]
    sessionLogs: TextArtifact[]
  }>({ prompts: [], sessionLogs: [] })
  const [jobSystemLogs, setJobSystemLogs] = useState<SystemLogLine[]>([])
  const [jobTab, setJobTab] = useState<JobTab>('overview')

  // Task / issue view
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null)
  const [taskTab, setTaskTab] = useState<TaskTab>('overview')

  const [cancelling, setCancelling] = useState(false)
  const [deletingJob, setDeletingJob] = useState(false)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [schedules, setSchedules] = useState<ScheduleItem[]>([])
  const [schedulesLoading, setSchedulesLoading] = useState(false)
  const [schedulesError, setSchedulesError] = useState<string | null>(null)
  const [scheduleCreating, setScheduleCreating] = useState(false)
  const [issueFilter, setIssueFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<JobStatusFilter>('all')
  const [density, setDensity] = useState<'comfortable' | 'compact'>('comfortable')
  const [jobsView, setJobsView] = useState<JobsPayload | null>(null)
  const [jobsPage, setJobsPage] = useState(1)
  const [jobsPageSize] = useState(25)
  const jobsPageRef = useRef(1)
  jobsPageRef.current = jobsPage
  const detailReturnTab = useRef<Tab>('tasks')
  const [settingsDirty, setSettingsDirty] = useState(false)
  const [modelsPayload, setModelsPayload] = useState<ModelsPayload | null>(null)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsFetchError, setModelsFetchError] = useState<string | null>(null)
  const issueFilterRef = useRef(issueFilter)
  issueFilterRef.current = issueFilter
  const detailRequestId = useRef(0)
  const jobsRequestId = useRef(0)
  const payloadGeneration = useRef(0)
  const lastAppliedServerTime = useRef<string | null>(null)
  const lastJobsReloadAt = useRef(0)
  /** Soft-refresh targets */
  const openJobIdRef = useRef<string | null>(null)
  const openIssueKeyRef = useRef<string | null>(null)
  const openViewModeRef = useRef<ViewMode>('list')
  const lastDetailRefreshRef = useRef(0)
  const reloadJobsRef = useRef<(opts?: { filter?: string; page?: number }) => Promise<void>>(
    async () => {},
  )

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
        setJobsError(null)
      } catch (e) {
        if (req !== jobsRequestId.current) return
        setJobsError(e instanceof Error ? e.message : 'Failed to load jobs')
      }
    },
    [jobsPageSize],
  )
  reloadJobsRef.current = reloadJobs

  const softRefreshOpenDetail = useCallback(async () => {
    const mode = openViewModeRef.current
    const req = detailRequestId.current
    try {
      if (mode === 'job' && openJobIdRef.current) {
        const jobId = openJobIdRef.current
        // Always hit job API so system_logs stay job-scoped
        const body = await fetchJobById(jobId)
        let job = asJobItem(body.job)
        const issue = body.issue
        if (req !== detailRequestId.current) return
        if (job) {
          setJobView(job)
          setJobArtifacts({
            prompts: issue?.prompts?.captured_prompt_files || [],
            sessionLogs: issue?.session_logs || [],
          })
          setJobSystemLogs(
            Array.isArray(body.system_logs)
              ? (body.system_logs as SystemLogLine[])
              : [],
          )
          setDetailStale(false)
        }
      } else if (mode === 'task' && openIssueKeyRef.current) {
        const d = await fetchTaskDetail(openIssueKeyRef.current)
        if (req !== detailRequestId.current) return
        setTaskDetail(d)
        setDetailStale(false)
      }
    } catch {
      setDetailStale(true)
    }
  }, [])

  const applyPayload = useCallback(
    (payload: DashboardPayload) => {
      const st = payload.poll?.server_time || payload.meta?.server_time || null
      if (st && lastAppliedServerTime.current && st < lastAppliedServerTime.current) {
        return
      }
      if (st) lastAppliedServerTime.current = st

      setData(payload)

      const now = Date.now()
      if (now - lastJobsReloadAt.current >= 1500) {
        lastJobsReloadAt.current = now
        void reloadJobsRef.current()
      }

      if (
        openViewModeRef.current !== 'list' &&
        now - lastDetailRefreshRef.current >= 4000
      ) {
        lastDetailRefreshRef.current = now
        void softRefreshOpenDetail()
      }
    },
    [softRefreshOpenDetail],
  )

  const loadDashboard = useCallback(() => {
    const gen = ++payloadGeneration.current
    fetchDashboard()
      .then((payload) => {
        if (gen !== payloadGeneration.current) return
        applyPayload(payload)
        setDashboardError(null)
        void reloadJobs()
      })
      .catch((e: Error) => {
        if (gen !== payloadGeneration.current) return
        setDashboardError(e.message)
      })
  }, [applyPayload, reloadJobs])

  const closeDetail = useCallback((opts?: { skipNavigate?: boolean }) => {
    detailRequestId.current += 1
    openJobIdRef.current = null
    openIssueKeyRef.current = null
    openViewModeRef.current = 'list'
    setViewMode('list')
    setJobView(null)
    setTaskDetail(null)
    setJobArtifacts({ prompts: [], sessionLogs: [] })
    setJobSystemLogs([])
    setDetailError(null)
    setDetailLoading(false)
    setDetailStale(false)
    if (!opts?.skipNavigate) {
      const back = detailReturnTab.current
      navigateTo(pathForTab(back))
      setTab(back)
    }
  }, [])

  const openJobDetail = async (
    jobId: string,
    issueKey?: string | null,
    opts?: { skipNavigate?: boolean; replace?: boolean; fromTab?: Tab },
  ) => {
    const jid = jobId.trim()
    if (!jid) return
    const req = ++detailRequestId.current
    openJobIdRef.current = jid
    openIssueKeyRef.current = issueKey?.trim().toUpperCase() || null
    openViewModeRef.current = 'job'
    if (opts?.fromTab) {
      detailReturnTab.current = opts.fromTab
    } else if (!opts?.skipNavigate) {
      detailReturnTab.current = tab
    }
    setViewMode('job')
    setDetailLoading(true)
    setDetailError(null)
    setJobTab('overview')
    setTaskDetail(null)
    if (!opts?.skipNavigate) {
      navigateTo(pathForJob(jid, issueKey), opts?.replace === true)
    }
    try {
      // Job API includes job-scoped system_logs (tagged with job_id)
      const body = await fetchJobById(jid)
      const job = asJobItem(body.job)
      const issue = body.issue
      if (job.issue_key) {
        openIssueKeyRef.current = job.issue_key.toUpperCase()
      }
      if (req !== detailRequestId.current) return
      if (!job.job_id) {
        throw new Error(`Job ${jid} not found`)
      }
      setJobView(job)
      setJobArtifacts({
        prompts: issue?.prompts?.captured_prompt_files || [],
        sessionLogs: issue?.session_logs || [],
      })
      setJobSystemLogs(
        Array.isArray(body.system_logs)
          ? (body.system_logs as SystemLogLine[])
          : [],
      )
      lastDetailRefreshRef.current = Date.now()
      // Prefer issue from URL if missing
      if (!opts?.skipNavigate && job.issue_key) {
        navigateTo(pathForJob(jid, job.issue_key), true)
      }
    } catch (e) {
      if (req !== detailRequestId.current) return
      setJobView(null)
      setJobSystemLogs([])
      setDetailError(e instanceof Error ? e.message : 'Failed to load job')
    } finally {
      if (req === detailRequestId.current) {
        setDetailLoading(false)
      }
    }
  }

  const openTaskDetail = async (
    issueKey: string,
    opts?: { skipNavigate?: boolean; replace?: boolean; fromTab?: Tab },
  ) => {
    const key = issueKey.trim().toUpperCase()
    if (!key) return
    const req = ++detailRequestId.current
    openJobIdRef.current = null
    openIssueKeyRef.current = key
    openViewModeRef.current = 'task'
    if (opts?.fromTab) {
      detailReturnTab.current = opts.fromTab
    } else if (!opts?.skipNavigate) {
      detailReturnTab.current = tab
    }
    setViewMode('task')
    setDetailLoading(true)
    setDetailError(null)
    setTaskTab('overview')
    setJobView(null)
    if (!opts?.skipNavigate) {
      navigateTo(pathForTask(key), opts?.replace === true)
    }
    try {
      const d = await fetchTaskDetail(key)
      if (req !== detailRequestId.current) return
      setTaskDetail(d)
      lastDetailRefreshRef.current = Date.now()
    } catch (e) {
      if (req !== detailRequestId.current) return
      setTaskDetail(null)
      setDetailError(e instanceof Error ? e.message : 'Failed to load issue')
    } finally {
      if (req === detailRequestId.current) {
        setDetailLoading(false)
      }
    }
  }

  const refreshDetail = async () => {
    if (viewMode === 'job' && openJobIdRef.current) {
      await openJobDetail(openJobIdRef.current, openIssueKeyRef.current, {
        skipNavigate: true,
      })
    } else if (viewMode === 'task' && openIssueKeyRef.current) {
      await openTaskDetail(openIssueKeyRef.current, { skipNavigate: true })
    }
  }

  useEffect(() => {
    const applyRoute = () => {
      const route = parseLocation()
      const nextTab = tabFromRoute(route)
      setTab(nextTab)
      if (route.kind === 'job') {
        void openJobDetail(route.jobId, route.issueKey, { skipNavigate: true })
      } else if (route.kind === 'task') {
        void openTaskDetail(route.issueKey, { skipNavigate: true })
      } else {
        closeDetail({ skipNavigate: true })
      }
    }
    applyRoute()
    window.addEventListener('popstate', applyRoute)
    return () => window.removeEventListener('popstate', applyRoute)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onCancelIssue = async () => {
    // Prefer task detail; fall back to job view (job page previously had no Cancel).
    const issueKey =
      (taskDetail?.issue_key || jobView?.issue_key || '').trim().toUpperCase()
    if (!issueKey) return
    if (taskDetail?.issue_key && !taskDetail.can_cancel) return
    if (
      jobView &&
      !taskDetail &&
      !jobView.live &&
      !['pending', 'planning', 'executing', 'running'].includes(
        (jobView.status || '').toLowerCase(),
      )
    ) {
      return
    }
    if (
      !window.confirm(
        `Cancel in-flight work for issue ${issueKey}?` +
          (jobView?.job_id ? `\n\nJob: ${jobView.job_id}` : ''),
      )
    )
      return
    setCancelling(true)
    setDetailError(null)
    try {
      await cancelTask(issueKey)
      await refreshDetail()
      void reloadJobs()
      const dash = await fetchDashboard()
      applyPayload(dash)
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : 'Cancel failed')
    } finally {
      setCancelling(false)
    }
  }

  const onDeleteJob = async () => {
    if (!jobView?.job_id) return
    const jid = jobView.job_id
    if (
      !window.confirm(
        `Permanently delete job ${jid}?\n\n` +
          'Removes the job history record and linked session/prompt files under .jira-agent. ' +
          'Does not change the Jira issue.',
      )
    ) {
      return
    }
    setDeletingJob(true)
    setDetailError(null)
    try {
      await deleteJob(jid, { deleteArtifacts: true })
      closeDetail()
      void reloadJobs()
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : 'Delete failed')
    } finally {
      setDeletingJob(false)
    }
  }

  const onBulkDeleteJobs = async (jobIds: string[]) => {
    if (jobIds.length === 0) return
    setBulkDeleting(true)
    setJobsError(null)
    try {
      const result = await deleteJobs(jobIds, { deleteArtifacts: true })
      if (result.failed_count > 0) {
        const sample = (result.failed || [])
          .slice(0, 3)
          .map((f) => `${f.job_id}: ${f.error}`)
          .join('; ')
        const more =
          result.failed_count > 3
            ? ` (+${result.failed_count - 3} more)`
            : ''
        if (result.deleted_count === 0) {
          throw new Error(
            result.message || `Could not delete jobs. ${sample}${more}`,
          )
        }
        setJobsError(
          `Deleted ${result.deleted_count}; ${result.failed_count} failed. ${sample}${more}`,
        )
      }
      await reloadJobs()
    } finally {
      setBulkDeleting(false)
    }
  }

  const reloadSchedules = useCallback(async () => {
    setSchedulesLoading(true)
    setSchedulesError(null)
    try {
      const payload = await fetchSchedules()
      setSchedules(payload.schedules || [])
    } catch (e) {
      setSchedulesError(
        e instanceof Error ? e.message : 'Failed to load schedules',
      )
    } finally {
      setSchedulesLoading(false)
    }
  }, [])

  const onCreateSchedule = async (body: ScheduleCreateBody) => {
    setScheduleCreating(true)
    setSchedulesError(null)
    try {
      await createSchedule(body)
      await reloadSchedules()
    } finally {
      setScheduleCreating(false)
    }
  }

  const onCancelSchedule = async (scheduleId: string) => {
    setSchedulesError(null)
    await cancelSchedule(scheduleId)
    await reloadSchedules()
  }

  useEffect(() => {
    const id = window.setInterval(() => setLocalClock(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])

  useEffect(() => {
    if (tab !== 'scheduled') return
    void reloadSchedules()
    const id = window.setInterval(() => void reloadSchedules(), 15000)
    return () => window.clearInterval(id)
  }, [tab, reloadSchedules])

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

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let retry: number | undefined

    const connect = () => {
      if (closed) return
      try {
        ws = new WebSocket(dashboardWsUrl())
        ws.onopen = () => {
          setConnected(true)
          void reloadJobsRef.current()
        }
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
    const creds = data.settings.gitlab_credentials ?? []
    setSettingsDraft({
      jira_host: data.settings.jira_host,
      jira_email: data.settings.jira_email ?? '',
      jira_board_id: data.settings.jira_board_id,
      poll_interval_seconds: data.settings.poll_interval_seconds,
      trigger_labels: data.settings.trigger_labels,
      trigger_on_assignment: data.settings.trigger_on_assignment,
      max_concurrent_jobs: data.settings.max_concurrent_jobs,
      agent_task_timeout_seconds: data.settings.agent_task_timeout_seconds,
      default_model: data.settings.default_model,
      gitlab_allowed_hosts: data.settings.gitlab_allowed_hosts ?? '',
      // Secrets are write-only — never hydrate from server
      jira_api_token: '',
      gitlab_pat: '',
      gitlab_cred_rows: creds.map((c) => ({
        host: c.host,
        pat: '',
        pat_configured: Boolean(c.pat_configured),
      })),
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

  useEffect(() => {
    if (tab !== 'settings') return
    void loadModels(false)
  }, [tab, loadModels])

  const displayTime = useMemo(() => localClock.toLocaleString(), [localClock])

  const tickLeft = useMemo(() => {
    const next = data?.poll?.next_poll_at
    if (!next) return data?.poll?.seconds_until_next_poll ?? null
    const ms = Date.parse(next)
    if (Number.isNaN(ms)) return data?.poll?.seconds_until_next_poll ?? null
    return Math.max(0, Math.floor((ms - localClock.getTime()) / 1000))
  }, [data?.poll?.next_poll_at, data?.poll?.seconds_until_next_poll, localClock])

  const onSaveSettings = async () => {
    setSaving(true)
    setSettingsError(null)
    try {
      const board = String(settingsDraft.jira_board_id ?? '').trim()
      if (!board) {
        throw new Error('Board ID is required (cannot be empty)')
      }
      const poll = Number(settingsDraft.poll_interval_seconds)
      const maxJobs = Number(settingsDraft.max_concurrent_jobs)
      const agentTimeout = Number(settingsDraft.agent_task_timeout_seconds)
      if (!Number.isFinite(poll) || poll < 5 || poll > 3600) {
        throw new Error('Poll interval must be between 5 and 3600 seconds')
      }
      if (!Number.isFinite(maxJobs) || maxJobs < 1 || maxJobs > 64) {
        throw new Error('Max concurrent jobs must be between 1 and 64')
      }
      if (!Number.isFinite(agentTimeout) || agentTimeout < 30 || agentTimeout > 86400) {
        throw new Error(
          'Agent/OpenCode timeout must be between 30 and 86400 seconds',
        )
      }
      const body: Parameters<typeof patchSettings>[0] = {
        jira_host: (settingsDraft.jira_host ?? '').trim(),
        jira_email: (settingsDraft.jira_email ?? '').trim(),
        jira_board_id: board,
        poll_interval_seconds: poll,
        trigger_labels: settingsDraft.trigger_labels,
        trigger_on_assignment: settingsDraft.trigger_on_assignment,
        max_concurrent_jobs: maxJobs,
        agent_task_timeout_seconds: agentTimeout,
        default_model: (settingsDraft.default_model ?? '').trim(),
      }
      // Write-only Jira token: only send when the operator typed a new value
      const jiraTok = (settingsDraft.jira_api_token ?? '').trim()
      if (jiraTok) body.jira_api_token = jiraTok

      // Per-host GitLab credentials (full replace list)
      const rows = settingsDraft.gitlab_cred_rows || []
      body.gitlab_credentials = rows
        .map((r) => ({
          host: (r.host || '').trim(),
          pat: (r.pat || '').trim() || undefined,
        }))
        .filter((r) => r.host)

      // Validate: new hosts need a PAT
      for (const r of rows) {
        const h = (r.host || '').trim()
        if (!h) continue
        if (!r.pat_configured && !(r.pat || '').trim()) {
          throw new Error(
            `GitLab host "${h}" needs a PAT (or remove the row)`,
          )
        }
      }

      const updated = await patchSettings(body)
      setSettingsDirty(false)
      setSettingsError(null)
      setData((prev) => (prev ? { ...prev, settings: updated } : prev))
      // Clear secret fields after successful save
      setSettingsDraft({
        jira_host: updated.jira_host,
        jira_email: updated.jira_email ?? '',
        jira_api_token: '',
        gitlab_pat: '',
        jira_board_id: updated.jira_board_id,
        poll_interval_seconds: updated.poll_interval_seconds,
        trigger_labels: updated.trigger_labels,
        trigger_on_assignment: updated.trigger_on_assignment,
        max_concurrent_jobs: updated.max_concurrent_jobs,
        agent_task_timeout_seconds: updated.agent_task_timeout_seconds,
        default_model: updated.default_model,
        gitlab_allowed_hosts: updated.gitlab_allowed_hosts ?? '',
        gitlab_cred_rows: (updated.gitlab_credentials || []).map((c) => ({
          host: c.host,
          pat: '',
          pat_configured: Boolean(c.pat_configured),
        })),
      })
      setModelsPayload((prev) =>
        prev ? { ...prev, default_model: updated.default_model } : prev,
      )
    } catch (e) {
      setSettingsError(e instanceof Error ? e.message : 'Settings save failed')
    } finally {
      setSaving(false)
    }
  }

  const backLabel =
    detailReturnTab.current === 'poll'
      ? 'poll monitor'
      : detailReturnTab.current === 'settings'
        ? 'settings'
        : detailReturnTab.current === 'scheduled'
          ? 'scheduled'
          : 'jobs'

  const detailOpen = viewMode === 'job' || viewMode === 'task'

  const navBtn = (id: Tab, label: string) => (
    <button
      key={id}
      type="button"
      onClick={() => {
        navigateTo(pathForTab(id))
        setTab(id)
        closeDetail({ skipNavigate: true })
      }}
      className={`rounded px-3 py-2 text-sm font-medium transition-colors ${
        tab === id && !detailOpen
          ? 'bg-accent text-white'
          : 'text-text-secondary hover:bg-surface-hover hover:text-text'
      }`}
    >
      {label}
    </button>
  )

  return (
    <div className="min-h-screen bg-bg text-text">
      <header className="border-b border-border bg-bg-elevated">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-4 py-3">
          <div>
            <h1 className="text-base font-semibold tracking-tight text-text">
              JIRA Virtual Developer
            </h1>
            <p className="text-xs text-text-muted">Operations dashboard</p>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <div className="rounded border border-border bg-surface px-3 py-1.5">
              <span className="text-text-muted">Version </span>
              <span className="font-mono text-text-secondary">
                {data?.meta.version ?? '—'}
              </span>
            </div>
            <div className="rounded border border-border bg-surface px-3 py-1.5 font-mono text-text-secondary">
              {displayTime}
            </div>
            <div
              className={`flex items-center gap-2 rounded border px-3 py-1.5 text-xs font-medium ${
                connected
                  ? 'border-border bg-surface text-success-text'
                  : 'border-border bg-surface text-warning-text'
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  connected ? 'bg-success' : 'bg-warning'
                }`}
              />
              {connected ? 'Live' : 'Reconnecting'}
            </div>
          </div>
        </div>
        <div className="mx-auto flex max-w-7xl gap-1 px-4 pb-2">
          {navBtn('tasks', 'Jobs')}
          {navBtn('scheduled', 'Scheduled')}
          {navBtn('poll', 'Poll monitor')}
          {navBtn('settings', 'Settings')}
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6">
        {(dashboardError || jobsError || settingsError || data?.poll?.error) && (
          <div className="space-y-2">
            {data?.poll?.error && (
              <div role="alert" className="ops-alert ops-alert-warning">
                <span className="font-medium">Poller error: </span>
                {data.poll.error}
              </div>
            )}
            {dashboardError && (
              <div
                role="alert"
                className="ops-alert ops-alert-danger flex flex-wrap items-center justify-between gap-3"
              >
                <span>{dashboardError}</span>
                <button
                  type="button"
                  onClick={() => {
                    setDashboardError(null)
                    loadDashboard()
                  }}
                  className="ops-btn ops-btn-secondary px-3 py-1 text-xs"
                >
                  Retry dashboard
                </button>
              </div>
            )}
            {jobsError && (
              <div
                role="alert"
                className="ops-alert ops-alert-danger flex flex-wrap items-center justify-between gap-3"
              >
                <span>Jobs: {jobsError}</span>
                <button
                  type="button"
                  onClick={() => {
                    setJobsError(null)
                    void reloadJobs()
                  }}
                  className="ops-btn ops-btn-secondary px-3 py-1 text-xs"
                >
                  Retry jobs
                </button>
              </div>
            )}
            {settingsError && (
              <div role="alert" className="ops-alert ops-alert-danger">
                Settings: {settingsError}
              </div>
            )}
          </div>
        )}

        {!data && !dashboardError && (
          <div className="text-sm text-text-muted" aria-busy="true">
            Loading dashboard…
          </div>
        )}

        {viewMode === 'job' && (
          <JobDetail
            job={jobView}
            artifacts={jobArtifacts}
            systemLogs={jobSystemLogs}
            loading={detailLoading}
            error={detailError}
            stale={detailStale}
            detailTab={jobTab}
            setDetailTab={setJobTab}
            backLabel={backLabel}
            onBack={() => closeDetail()}
            onRefresh={() => void refreshDetail()}
            onOpenTask={(key) => void openTaskDetail(key)}
            onCancel={() => void onCancelIssue()}
            cancelling={cancelling}
            onDelete={() => void onDeleteJob()}
            deleting={deletingJob}
          />
        )}

        {viewMode === 'task' && (
          <TaskDetailPage
            detail={taskDetail}
            loading={detailLoading}
            error={detailError}
            stale={detailStale}
            detailTab={taskTab}
            setDetailTab={setTaskTab}
            backLabel={backLabel}
            onBack={() => closeDetail()}
            onRefresh={() => void refreshDetail()}
            onOpenJob={(key, jobId) => void openJobDetail(jobId, key)}
            onCancel={() => void onCancelIssue()}
            cancelling={cancelling}
          />
        )}

        {data && tab === 'tasks' && viewMode === 'list' && (
          <JobsPage
            jobsView={jobsView}
            jobsPage={jobsPage}
            jobsPageSize={jobsPageSize}
            issueFilter={issueFilter}
            setIssueFilter={setIssueFilter}
            statusFilter={statusFilter}
            setStatusFilter={setStatusFilter}
            density={density}
            setDensity={setDensity}
            setJobsPage={setJobsPage}
            connected={connected}
            onOpenJob={(key, jobId) => void openJobDetail(jobId, key)}
            onOpenIssue={(key) => void openTaskDetail(key)}
            onBulkDelete={onBulkDeleteJobs}
            bulkDeleting={bulkDeleting}
          />
        )}

        {tab === 'scheduled' && viewMode === 'list' && (
          <ScheduledPage
            schedules={schedules}
            loading={schedulesLoading}
            error={schedulesError}
            creating={scheduleCreating}
            onCreate={onCreateSchedule}
            onCancel={onCancelSchedule}
            onRefresh={() => void reloadSchedules()}
            onOpenIssue={(key) =>
              void openTaskDetail(key, { fromTab: 'scheduled' })
            }
          />
        )}

        {data && tab === 'poll' && viewMode === 'list' && (
          <PollMonitor
            data={data}
            tickLeft={tickLeft}
            onOpenIssue={(key) =>
              void openTaskDetail(key, { fromTab: 'poll' })
            }
          />
        )}

        {data && tab === 'settings' && viewMode === 'list' && (
          <SettingsPage
            data={data}
            settingsDraft={settingsDraft}
            setSettingsDraft={setSettingsDraft}
            setSettingsDirty={setSettingsDirty}
            modelsPayload={modelsPayload}
            modelsLoading={modelsLoading}
            modelsFetchError={modelsFetchError}
            loadModels={(r) => void loadModels(r)}
            saving={saving}
            onSave={() => void onSaveSettings()}
          />
        )}
      </main>
    </div>
  )
}
