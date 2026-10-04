import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  cancelSchedule,
  createSchedule,
  dispatchSchedule,
  fetchAzureProjects,
  fetchIssueTypes,
  fetchSchedules,
  isAbortError,
  patchSettings,
  previewScheduleIssue,
  previewScheduleMr,
  previewSchedulePr,
  scheduleExistingIssue,
  scheduleMrFollowup,
  schedulePrFollowup,
} from '../../api/client'
import type {
  JiraIssueType,
  ProjectRepository,
  ScheduleItem,
  ScheduleMrPreview,
  SchedulePrPreview,
  SchedulePreview,
  WorkMode,
} from '../../api/types'
import { useLive } from '../../app/live'
import { noteLiveGeneration, usePageLoad } from '../../api/pageLoad'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { ModelField } from '../../ui/ModelField'
import { PageHeader } from '../../ui/PageHeader'
import { SavedRepoSearch } from '../../ui/ProjectSelect'
import { Spinner } from '../../ui/Spinner'
import { StatusBadge } from '../../ui/StatusBadge'
import {
  datetimeLocalToNaiveIso,
  formatScheduleWhen,
  joinDatetimeLocal,
  localNaiveNowIso,
  splitDatetimeLocal,
} from '../../util/time'
import { withListPage } from '../../util/listPageUrl'
import {
  canonicalSchedulePath,
  parseSchedulePath,
  scheduleHere,
  schedulePath,
  type ScheduleMode,
  type ScheduleTracker,
} from './scheduleTabUrl'
import {
  RepositoryList,
  emptyRepoRow,
  scheduleRepositoryFields,
  type RepoRow,
} from './MoreRepositories'

const LAST_REPO_KEY = 'vd.schedule.last_repo_url'
const CUSTOM_REPO = '__custom__'
const PAGE_SIZE = 25
const BUILTIN_MODES = ['build', 'plan', 'test']

function scheduleModeNames(saved: WorkMode[] | undefined, current: string): string[] {
  const names: string[] = []
  const seen = new Set<string>()
  const push = (raw: string) => {
    const name = raw.trim().toLowerCase()
    if (!name || seen.has(name)) return
    seen.add(name)
    names.push(name)
  }
  for (const row of saved || []) push(row.name)
  if (!names.length) {
    for (const name of BUILTIN_MODES) push(name)
  }
  push(current)
  return names
}

/** Picker default for "schedule later" only — not used by Run now. */
function defaultWhen(): string {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset() + 5)
  d.setSeconds(0, 0)
  return d.toISOString().slice(0, 16)
}

function scheduledAtForSubmit(when: string, dispatchNow: boolean): string {
  return dispatchNow ? localNaiveNowIso() : datetimeLocalToNaiveIso(when)
}

export function SchedulesPage() {
  const { mode: modeParam = '', tracker: trackerParam = '', page: pageParam = '' } = useParams()
  const navigate = useNavigate()
  const parsed = parseSchedulePath(modeParam, trackerParam, pageParam)
  const mode: ScheduleMode = parsed?.mode ?? 'existing'
  const tracker: ScheduleTracker = parsed?.tracker ?? 'jira'
  const page = parsed?.page ?? 1
  const live = useLive()
  const [rows, setRows] = useState<ScheduleItem[]>([])
  const [total, setTotal] = useState(0)
  const [pageSize, setPageSize] = useState(PAGE_SIZE)
  const [shownPage, setShownPage] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const lastGenReload = useRef(0)
  const genSeen = useRef<number | null>(null)
  const flight = usePageLoad()

  useEffect(() => {
    const want = canonicalSchedulePath(modeParam, trackerParam, pageParam)
    const here = scheduleHere(modeParam, trackerParam, pageParam)
    if (here !== want) navigate(want, { replace: true })
  }, [modeParam, navigate, pageParam, trackerParam])

  const reload = useCallback(async (loadMode: 'query' | 'tick' = 'query', pageOverride?: number) => {
    const nextPage = pageOverride ?? page
    const key = String(nextPage)
    const signal = loadMode === 'tick' ? flight.tick(key) : flight.query(key)
    if (!signal) return
    if (loadMode === 'query') setError(null)
    try {
      const p = await fetchSchedules({ page: nextPage, pageSize: PAGE_SIZE, signal })
      if (signal.aborted) return
      const size = p.page_size ?? PAGE_SIZE
      const count = p.total ?? 0
      const pages = Math.max(1, Math.ceil(count / size) || 1)
      const landed = p.page ?? nextPage
      setRows(p.schedules || [])
      setTotal(count)
      setPageSize(size)
      setShownPage(nextPage)
      setError(null)
      if (landed > pages) {
        navigate(withListPage(schedulePath(mode, tracker), pages), { replace: true })
      }
    } catch (e) {
      if (signal.aborted || isAbortError(e)) return
      setShownPage(nextPage)
      setError(e instanceof Error ? e.message : 'Load failed')
    } finally {
      if (flight.settle(signal)) void reload('tick', nextPage)
    }
  }, [flight, mode, navigate, page, tracker])

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

  const listPending = shownPage !== page
  const currentPage = page
  const size = pageSize || PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (currentPage - 1) * size + 1
  const to = Math.min(currentPage * size, total)

  return (
    <section className="space-y-5">
      <PageHeader
        title="Scheduled"
        description={
          mode === 'mr' || mode === 'pr'
            ? 'At the chosen time the prompt is posted on the request. The answer is posted when the worker finishes.'
            : 'Queue a run for a chosen time.'
        }
      />
      <div className="vd-seg">
        <button
          type="button"
          className={`vd-seg-btn ${mode === 'existing' ? 'is-on' : ''}`}
          aria-pressed={mode === 'existing'}
          onClick={() => navigate(schedulePath('existing'))}
        >
          Existing issue
        </button>
        <button
          type="button"
          className={`vd-seg-btn ${mode === 'new' ? 'is-on' : ''}`}
          aria-pressed={mode === 'new'}
          onClick={() => navigate(schedulePath('new'))}
        >
          New issue
        </button>
        <button
          type="button"
          className={`vd-seg-btn ${mode === 'mr' ? 'is-on' : ''}`}
          aria-pressed={mode === 'mr'}
          onClick={() => navigate(schedulePath('mr'))}
        >
          Existing MR
        </button>
        <button
          type="button"
          className={`vd-seg-btn ${mode === 'pr' ? 'is-on' : ''}`}
          aria-pressed={mode === 'pr'}
          onClick={() => navigate(schedulePath('pr'))}
        >
          Existing PR
        </button>
      </div>
      {mode === 'existing' ? (
        <Existing onDone={() => {
          if (page > 1) navigate(schedulePath('existing', tracker))
          else void reload()
        }} />
      ) : mode === 'mr' ? (
        <ExistingMr onDone={() => {
          if (page > 1) navigate(schedulePath('mr'))
          else void reload()
        }} />
      ) : mode === 'pr' ? (
        <ExistingPr onDone={() => {
          if (page > 1) navigate(schedulePath('pr'))
          else void reload()
        }} />
      ) : (
        <CreateNew onDone={() => {
          if (page > 1) navigate(schedulePath('new', tracker))
          else void reload()
        }} />
      )}
      {error && <p className="text-sm text-danger-text">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2 text-xs text-text-muted">
        <span>
          {from}–{to} of {total}
        </span>
        <button
          type="button"
          disabled={currentPage <= 1}
          onClick={() => navigate(withListPage(schedulePath(mode, tracker), currentPage - 1))}
          className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
        >
          Prev
        </button>
        <button
          type="button"
          disabled={currentPage >= totalPages}
          onClick={() => navigate(withListPage(schedulePath(mode, tracker), currentPage + 1))}
          className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
        >
          Next
        </button>
      </div>
      <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
        {!listPending && rows.map((s) => (
          <li key={s.schedule_id} className="py-3 text-sm">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <div className="min-w-0">
                {s.issue_key ? (
                  <Link className="font-mono text-accent-text hover:underline" to={`/tasks/${encodeURIComponent(s.issue_key)}`}>
                    {s.issue_key}
                  </Link>
                ) : (
                  <span className="text-text-muted">—</span>
                )}{' '}
                {s.source === 'gitlab_mr' && s.mr_iid ? (
                  s.merge_request_url ? (
                    <a
                      className="font-mono text-accent-text hover:underline"
                      href={s.merge_request_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      !{s.mr_iid}
                    </a>
                  ) : (
                    <span className="font-mono">!{s.mr_iid}</span>
                  )
                ) : null}{' '}
                {s.source === 'azure_pr' && s.pr_id ? (
                  s.merge_request_url ? (
                    <a
                      className="font-mono text-accent-text hover:underline"
                      href={s.merge_request_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      !{s.pr_id}
                    </a>
                  ) : (
                    <span className="font-mono">!{s.pr_id}</span>
                  )
                ) : null}{' '}
                <span className="text-text">{s.title}</span>
              </div>
              <StatusBadge status={s.status} size="sm" />
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-muted">
              <span>
                {s.source === 'gitlab_mr'
                  ? 'MR follow-up'
                  : s.source === 'azure_pr'
                    ? 'PR follow-up'
                    : s.mode}
              </span>
              <span>{formatScheduleWhen(s.scheduled_at)}</span>
              {(s.status === 'scheduled' || s.status === 'error') && (
                <button
                  type="button"
                  className="vd-btn-ghost text-accent-text"
                  onClick={() => setRunId(s.schedule_id)}
                >
                  Run now
                </button>
              )}
              {(s.status === 'scheduled' || s.status === 'error') && (
                <button
                  type="button"
                  className="vd-btn-ghost text-danger-text"
                  onClick={() => setCancelId(s.schedule_id)}
                >
                  Cancel
                </button>
              )}
            </div>
          </li>
        ))}
        {(listPending || rows.length === 0) && (
          <li className="py-6 text-text-muted" aria-busy={listPending && !error}>
            {listPending && !error ? (
              <span className="inline-flex items-center gap-2">
                <Spinner /> Loading schedules…
              </span>
            ) : (
              'Nothing scheduled.'
            )}
          </li>
        )}
      </ul>
      <ConfirmDialog
        open={Boolean(runId)}
        title="Run this job now?"
        body="Starts agent work immediately. Does not wait for the scheduled time."
        confirmLabel="Run now"
        busy={busy}
        onConfirm={async () => {
          if (!runId) return
          setBusy(true)
          try {
            await dispatchSchedule(runId)
            setRunId(null)
            await reload()
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setRunId(null)}
      />
      <ConfirmDialog
        open={Boolean(cancelId)}
        title="Cancel this schedule?"
        body={
          rows.find((s) => s.schedule_id === cancelId)?.source === 'gitlab_mr'
            ? 'Does not close the merge request or delete posted notes.'
            : rows.find((s) => s.schedule_id === cancelId)?.source === 'azure_pr'
              ? 'Does not close the pull request or delete posted comments.'
              : (() => {
                  const k = rows.find((s) => s.schedule_id === cancelId)?.issue_key || ''
                  return /^\d+$/.test(k) || k.startsWith('WIT-')
                    ? 'Does not delete the Azure work item.'
                    : 'Does not delete the Jira issue.'
                })()
        }
        confirmLabel="Cancel it"
        danger
        busy={busy}
        onConfirm={async () => {
          if (!cancelId) return
          setBusy(true)
          try {
            await cancelSchedule(cancelId)
            setCancelId(null)
            await reload()
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setCancelId(null)}
      />
    </section>
  )
}

function ExistingMr({ onDone }: { onDone: () => void }) {
  const live = useLive()
  const [projects, setProjects] = useState<ProjectRepository[]>(
    live.settings?.project_repositories || [],
  )
  const [repo, setRepo] = useState('')
  const [repoPick, setRepoPick] = useState(CUSTOM_REPO)
  const [iid, setIid] = useState('')
  const [preview, setPreview] = useState<ScheduleMrPreview | null>(null)
  const [prompt, setPrompt] = useState('')
  const [when, setWhen] = useState(defaultWhen)
  const [model, setModel] = useState('')
  const [backend, setBackend] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [looking, setLooking] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const seeded = useRef(false)

  useEffect(() => {
    const rows = live.settings?.project_repositories
    if (rows) setProjects(rows)
  }, [live.settings])

  useEffect(() => {
    if (seeded.current) return
    const rows = live.settings?.project_repositories || []
    if (!rows.length) return
    seeded.current = true
    const last = (() => {
      try {
        return window.localStorage.getItem(LAST_REPO_KEY) || ''
      } catch {
        return ''
      }
    })()
    const preferred = rows.find((p) => p.url === last) || (rows.length === 1 ? rows[0] : null)
    if (preferred) {
      setRepoPick(preferred.url)
      setRepo(preferred.url)
    }
  }, [live.settings])

  const get = async () => {
    setErr(null)
    setLooking(true)
    try {
      const n = Number(iid)
      const p = await previewScheduleMr(repo.trim(), Number.isFinite(n) ? n : 0)
      setPreview(p)
      setModelsLoading(true)
      if (p.repository_url) setRepo(p.repository_url)
      if (p.mr_iid) setIid(String(p.mr_iid))
    } catch (e) {
      setPreview(null)
      setPrompt('')
      setModel('')
      setBackend('')
      setErr(e instanceof Error ? e.message : 'MR lookup failed')
    } finally {
      setLooking(false)
    }
  }

  const submit = async (e: FormEvent, dispatchNow = false) => {
    e.preventDefault()
    if (!preview || modelsLoading) return
    const n = Number(iid)
    if (!repo.trim()) {
      setErr('Pick a project or enter a repository URL')
      return
    }
    if (!Number.isFinite(n) || n < 1) {
      setErr('MR iid must be a positive number')
      return
    }
    if (!prompt.trim()) {
      setErr('Prompt is required')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await scheduleMrFollowup({
        repository_url: repo.trim(),
        mr_iid: n,
        prompt: prompt.trim(),
        scheduled_at: scheduledAtForSubmit(when, dispatchNow),
        dispatch_now: dispatchNow,
        model: model.trim() || undefined,
        backend: backend.trim() || undefined,
      })
      try {
        window.localStorage.setItem(LAST_REPO_KEY, repo.trim())
      } catch {
        /* ignore */
      }
      setPreview(null)
      setPrompt('')
      setIid('')
      onDone()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const isCustom = repoPick === CUSTOM_REPO || projects.length === 0

  return (
    <form onSubmit={(e) => void submit(e)}>
      {projects.length > 0 && (
        <SavedRepoSearch
          label="Project"
          projects={projects}
          selectedUrl={repoPick === CUSTOM_REPO ? '' : repoPick}
          onPick={(v) => {
            setRepoPick(v)
            setPreview(null)
            if (v === CUSTOM_REPO) {
              setRepo('')
              return
            }
            const hit = projects.find((p) => p.url === v)
            if (hit) setRepo(hit.url)
          }}
          trailing={[{ value: CUSTOM_REPO, label: 'Other URL…' }]}
        />
      )}
      {(isCustom || projects.length === 0) && (
        <label className="field">
          <span>Repository</span>
          <input
            value={repo}
            onChange={(e) => {
              setRepo(e.target.value)
              setPreview(null)
            }}
            placeholder="https://gitlab.com/group/repo.git"
            required
          />
        </label>
      )}
      {!isCustom && repo ? <p className="quiet font-mono text-xs">{repo}</p> : null}
      <label className="field">
        <span>MR iid</span>
        <input
          value={iid}
          onChange={(e) => {
            setIid(e.target.value.replace(/[^\d]/g, ''))
            setPreview(null)
          }}
          inputMode="numeric"
          placeholder="12"
          required
        />
      </label>
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={looking || !repo.trim() || !iid.trim()}
          onClick={() => void get()}
        >
          {looking ? 'Looking up…' : 'Look up'}
        </button>
      </p>
      {preview && (
        <>
          <p className="quiet">
            {preview.gitlab_project}!{preview.mr_iid} — {preview.title}
            {preview.source_branch ? ` · ${preview.source_branch} → ${preview.target_branch}` : ''}
            {preview.issue_key ? ` · ${preview.issue_key}` : ''}
          </p>
          <label className="field">
            <span>Prompt</span>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={10}
              className="min-h-[10rem] font-mono text-xs"
              required
            />
            <span className="mt-1 block text-xs text-text-muted">
              Posted on the MR as a *Yaver* note marked “written in the ops
              dashboard”. The agent answer is posted there when the worker finishes.
            </span>
          </label>
          <WorkerBlock
            backend={backend}
            setBackend={setBackend}
            model={model}
            setModel={setModel}
            fallbackBackend={live.settings?.agent_backend || 'opencode'}
            fallbackModel={live.settings?.default_model || ''}
            setModelsLoading={setModelsLoading}
          />
          <ScheduleWhenField value={when} onChange={setWhen} />
          <p className="actions">
            <button type="submit" className="go" disabled={busy || modelsLoading}>
              {busy ? (
                <>
                  <Spinner /> Scheduling…
                </>
              ) : modelsLoading ? (
                <>
                  <Spinner /> Loading models…
                </>
              ) : (
                'Schedule'
              )}
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              disabled={busy || modelsLoading}
              onClick={(e) => void submit(e, true)}
            >
              Run now
            </button>
          </p>
        </>
      )}
      {err && <p className="err">{err}</p>}
    </form>
  )
}

function ExistingPr({ onDone }: { onDone: () => void }) {
  const live = useLive()
  const projects = useMemo(
    () => (live.settings?.project_repositories || []).filter((p) => /\/_git\//i.test(p.url || '')),
    [live.settings],
  )
  const [repo, setRepo] = useState('')
  const [repoPick, setRepoPick] = useState(CUSTOM_REPO)
  const [iid, setIid] = useState('')
  const [preview, setPreview] = useState<SchedulePrPreview | null>(null)
  const [prompt, setPrompt] = useState('')
  const [when, setWhen] = useState(defaultWhen)
  const [model, setModel] = useState('')
  const [backend, setBackend] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [looking, setLooking] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const seeded = useRef(false)

  useEffect(() => {
    if (seeded.current) return
    if (!projects.length) return
    seeded.current = true
    const last = (() => {
      try {
        return window.localStorage.getItem(LAST_REPO_KEY) || ''
      } catch {
        return ''
      }
    })()
    const preferred = projects.find((p) => p.url === last) || (projects.length === 1 ? projects[0] : null)
    if (preferred) {
      setRepoPick(preferred.url)
      setRepo(preferred.url)
    }
  }, [projects])

  const get = async () => {
    setErr(null)
    setLooking(true)
    try {
      const n = Number(iid)
      const p = await previewSchedulePr(repo.trim(), Number.isFinite(n) ? n : 0)
      setPreview(p)
      setModelsLoading(true)
      if (p.repository_url) setRepo(p.repository_url)
      if (p.pr_id) setIid(String(p.pr_id))
    } catch (e) {
      setPreview(null)
      setPrompt('')
      setModel('')
      setBackend('')
      setErr(e instanceof Error ? e.message : 'PR lookup failed')
    } finally {
      setLooking(false)
    }
  }

  const submit = async (e: FormEvent, dispatchNow = false) => {
    e.preventDefault()
    if (!preview || modelsLoading) return
    const n = Number(iid)
    if (!repo.trim()) {
      setErr('Pick a project or enter a repository URL')
      return
    }
    if (!Number.isFinite(n) || n < 1) {
      setErr('PR id must be a positive number')
      return
    }
    if (!prompt.trim()) {
      setErr('Prompt is required')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await schedulePrFollowup({
        repository_url: repo.trim(),
        pr_id: n,
        prompt: prompt.trim(),
        scheduled_at: scheduledAtForSubmit(when, dispatchNow),
        dispatch_now: dispatchNow,
        model: model.trim() || undefined,
        backend: backend.trim() || undefined,
      })
      try {
        window.localStorage.setItem(LAST_REPO_KEY, repo.trim())
      } catch {
        /* ignore */
      }
      setPreview(null)
      setPrompt('')
      setIid('')
      onDone()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const isCustom = repoPick === CUSTOM_REPO || projects.length === 0

  return (
    <form onSubmit={(e) => void submit(e)}>
      {projects.length > 0 && (
        <SavedRepoSearch
          label="Project"
          projects={projects}
          selectedUrl={repoPick === CUSTOM_REPO ? '' : repoPick}
          onPick={(v) => {
            setRepoPick(v)
            setPreview(null)
            if (v === CUSTOM_REPO) {
              setRepo('')
              return
            }
            const hit = projects.find((p) => p.url === v)
            if (hit) setRepo(hit.url)
          }}
          trailing={[{ value: CUSTOM_REPO, label: 'Other URL…' }]}
        />
      )}
      {(isCustom || projects.length === 0) && (
        <label className="field">
          <span>Repository</span>
          <input
            value={repo}
            onChange={(e) => {
              setRepo(e.target.value)
              setPreview(null)
            }}
            placeholder="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
            required
          />
        </label>
      )}
      {!isCustom && repo ? <p className="quiet font-mono text-xs">{repo}</p> : null}
      <label className="field">
        <span>PR id</span>
        <input
          value={iid}
          onChange={(e) => {
            setIid(e.target.value.replace(/[^\d]/g, ''))
            setPreview(null)
          }}
          inputMode="numeric"
          placeholder="12"
          required
        />
      </label>
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={looking || !repo.trim() || !iid.trim()}
          onClick={() => void get()}
        >
          {looking ? 'Looking up…' : 'Look up'}
        </button>
      </p>
      {preview && (
        <>
          <p className="quiet">
            {preview.azure_project}/{preview.azure_repository}!{preview.pr_id} — {preview.title}
            {preview.source_branch ? ` · ${preview.source_branch} → ${preview.target_branch}` : ''}
            {preview.issue_key ? ` · ${preview.issue_key}` : ''}
          </p>
          <label className="field">
            <span>Prompt</span>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={10}
              className="min-h-[10rem] font-mono text-xs"
              required
            />
            <span className="mt-1 block text-xs text-text-muted">
              Posted on the PR as a *Yaver* comment marked “written in the ops
              dashboard”. The agent answer is posted there when the worker finishes.
            </span>
          </label>
          <WorkerBlock
            backend={backend}
            setBackend={setBackend}
            model={model}
            setModel={setModel}
            fallbackBackend={live.settings?.agent_backend || 'opencode'}
            fallbackModel={live.settings?.default_model || ''}
            setModelsLoading={setModelsLoading}
          />
          <ScheduleWhenField value={when} onChange={setWhen} />
          <p className="actions">
            <button type="submit" className="go" disabled={busy || modelsLoading}>
              {busy ? (
                <>
                  <Spinner /> Scheduling…
                </>
              ) : modelsLoading ? (
                <>
                  <Spinner /> Loading models…
                </>
              ) : (
                'Schedule'
              )}
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              disabled={busy || modelsLoading}
              onClick={(e) => void submit(e, true)}
            >
              Run now
            </button>
          </p>
        </>
      )}
      {err && <p className="err">{err}</p>}
    </form>
  )
}

function azureCollectionsFromSettings(
  settings: { azure_collection_urls?: string[]; azure_credentials?: { collection_url?: string; host?: string }[] } | null | undefined,
): string[] {
  const fromList = settings?.azure_collection_urls || []
  const fromCreds = (settings?.azure_credentials || [])
    .map((c) => (c.collection_url || c.host || '').trim())
    .filter((u) => /^https?:\/\//i.test(u))
  const out: string[] = []
  for (const url of [...fromList, ...fromCreds]) {
    if (url && !out.includes(url)) out.push(url)
  }
  return out
}

function Existing({ onDone }: { onDone: () => void }) {
  const { mode: modeParam = '', tracker: trackerParam = '' } = useParams()
  const navigate = useNavigate()
  const tracker = parseSchedulePath(modeParam, trackerParam)?.tracker ?? 'jira'
  const live = useLive()
  const collections = azureCollectionsFromSettings(live.settings)
  const [collection, setCollection] = useState('')
  const [witId, setWitId] = useState('')
  const [key, setKey] = useState('')
  const [preview, setPreview] = useState<SchedulePreview | null>(null)
  const [prompt, setPrompt] = useState('')
  const [when, setWhen] = useState(defaultWhen)
  const [model, setModel] = useState('')
  const [backend, setBackend] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [looking, setLooking] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [projects, setProjects] = useState<ProjectRepository[]>(
    live.settings?.project_repositories || [],
  )
  const [repos, setRepos] = useState<RepoRow[]>([emptyRepoRow()])
  const [mode, setMode] = useState('build')

  const needsParams = Boolean(preview && !preview.template_valid)

  useEffect(() => {
    const rows = live.settings?.project_repositories
    if (rows) setProjects(rows)
  }, [live.settings])

  const seedPicker = (p: SchedulePreview, rows: ProjectRepository[]) => {
    const last = (() => {
      try {
        return window.localStorage.getItem(LAST_REPO_KEY) || ''
      } catch {
        return ''
      }
    })()
    const fromIssue = (p.repository_url || '').trim()
    const preferred =
      rows.find((r) => r.url === fromIssue) ||
      rows.find((r) => r.url === last) ||
      (rows.length === 1 ? rows[0] : null)
    const lookedUpSource = (p.source_branch || '').trim()
    const feature = featureBranchForKey(p.issue_key || '')
    const asRow = (url: string, source: string, target: string): RepoRow => {
      const branch = source.trim()
      const custom = Boolean(branch) && branch.toLowerCase() !== feature.toLowerCase()
      return {
        url,
        source: custom ? branch : 'develop',
        target: target.trim() || 'develop',
        sourceMode: custom ? 'custom' : 'issue_key',
      }
    }
    const loaded = (p.repository_refs || []).filter((row) => (row.url || '').trim())
    if (loaded.length > 1) {
      setRepos(loaded.map((row) => asRow(row.url, row.source_branch, row.target_branch)))
    } else {
      const custom =
        Boolean(lookedUpSource) && lookedUpSource.toLowerCase() !== feature.toLowerCase()
      const url = fromIssue || preferred?.url || ''
      setRepos([
        {
          url,
          source: custom ? lookedUpSource : preferred?.source_branch || 'develop',
          target: (p.target_branch || preferred?.target_branch || 'develop').trim(),
          sourceMode: custom ? 'custom' : 'issue_key',
        },
      ])
    }
    if ((p.mode || '').trim()) setMode(p.mode.trim().toLowerCase())
  }

  useEffect(() => {
    if (collection || !collections.length) return
    setCollection(collections[0] || '')
  }, [collections, collection])

  const get = async () => {
    setErr(null)
    setLooking(true)
    try {
      const p =
        tracker === 'azure'
          ? await previewScheduleIssue('', {
              collection_url: collection.trim(),
              work_item_id: Number(witId.trim()),
            })
          : await previewScheduleIssue(key.trim().toUpperCase())
      setPreview(p)
      setModelsLoading(true)
      setKey(p.issue_key || key)
      setPrompt(p.prompt || '')
      setModel(p.model || '')
      setBackend(p.backend || '')
      const rows = live.settings?.project_repositories || projects
      seedPicker(p, rows)
      if (!p.template_valid && p.error) setErr(null)
      else if (!p.ok && p.error) setErr(p.error)
    } catch (e) {
      setPreview(null)
      setPrompt('')
      setErr(e instanceof Error ? e.message : 'Preview failed')
    } finally {
      setLooking(false)
    }
  }

  const submit = async (e: FormEvent, dispatchNow = false) => {
    e.preventDefault()
    if (!preview || modelsLoading) return
    const picked = scheduleRepositoryFields(repos)
    if (!picked.repository_url) {
      setErr('Add a repository')
      return
    }
    if (!picked.target_branch) {
      setErr('Enter a target branch')
      return
    }
    if (picked.source_branch_mode === 'custom' && !picked.source_branch) {
      setErr('Enter a source branch')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await scheduleExistingIssue({
        issue_key: preview.issue_key || key.trim().toUpperCase(),
        scheduled_at: scheduledAtForSubmit(when, dispatchNow),
        dispatch_now: dispatchNow,
        model: model.trim() || undefined,
        backend: backend.trim() || undefined,
        description: prompt,
        repository_url: picked.repository_url,
        source_branch: picked.source_branch,
        target_branch: picked.target_branch,
        mode,
        source_branch_mode: picked.source_branch_mode,
        repository_refs: picked.repository_refs,
      })
      setRepos([emptyRepoRow()])
      setPreview(null)
      setKey('')
      setPrompt('')
      setModel('')
      setBackend('')
      onDone()
    } catch (err2) {
      setErr(err2 instanceof Error ? err2.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const loaded = Boolean(preview && (preview.ok || preview.title || prompt))

  const canLookUp =
    tracker === 'azure'
      ? Boolean(collection.trim() && Number(witId.trim()) > 0)
      : Boolean(key.trim())

  return (
    <form onSubmit={(e) => void submit(e)}>
      <div className="vd-seg mb-3">
        <button
          type="button"
          className={`vd-seg-btn ${tracker === 'jira' ? 'is-on' : ''}`}
          aria-pressed={tracker === 'jira'}
          onClick={() => {
            navigate(schedulePath('existing', 'jira'))
            setPreview(null)
          }}
        >
          Jira
        </button>
        <button
          type="button"
          className={`vd-seg-btn ${tracker === 'azure' ? 'is-on' : ''}`}
          aria-pressed={tracker === 'azure'}
          onClick={() => {
            navigate(schedulePath('existing', 'azure'))
            setPreview(null)
          }}
        >
          Azure work item
        </button>
      </div>
      {tracker === 'jira' ? (
        <label className="field">
          <span>Issue key</span>
          <input value={key} onChange={(e) => setKey(e.target.value.toUpperCase())} />
        </label>
      ) : (
        <>
          <label className="field">
            <span>Collection</span>
            {collections.length > 0 ? (
              <select
                value={collection}
                onChange={(e) => {
                  setCollection(e.target.value)
                  setPreview(null)
                }}
              >
                {collections.map((url) => (
                  <option key={url} value={url}>
                    {url}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={collection}
                onChange={(e) => {
                  setCollection(e.target.value)
                  setPreview(null)
                }}
                placeholder="https://tfs.example.com/tfs/DefaultCollection"
              />
            )}
          </label>
          {collections.length === 0 ? (
            <p className="quiet text-xs">
              Save a collection URL under Settings → Azure first, or paste one
              here.
            </p>
          ) : null}
          <label className="field">
            <span>Work item ID</span>
            <input
              value={witId}
              onChange={(e) => {
                setWitId(e.target.value.replace(/[^\d]/g, ''))
                setPreview(null)
              }}
              inputMode="numeric"
              placeholder="12345"
            />
          </label>
        </>
      )}
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={looking || !canLookUp}
          onClick={() => void get()}
        >
          {looking ? 'Looking up…' : 'Look up'}
        </button>
      </p>
      {loaded && preview && (
        <>
          <p className="quiet">
            {preview.issue_key} — {preview.title}
            {preview.jira_status ? ` · ${preview.jira_status}` : ''}
            {preview.template_valid
              ? ` · ${preview.mode} · ${preview.repository_url}`
              : ' · {params} missing or invalid'}
          </p>
          {needsParams && preview.message ? (
            <p className="quiet text-xs">{preview.message}</p>
          ) : null}
          <label className="field">
            <span>{tracker === 'azure' ? 'Work item prompt' : 'Jira prompt'}</span>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={needsParams ? 8 : 14}
              className="min-h-[12rem] font-mono text-xs"
            />
            <span className="mt-1 block text-xs text-text-muted">
              Ticket text only. Repository, source, target, and mode are the fields
              below. Schedule or Run now writes them back to Jira when you change them.
            </span>
          </label>
          <RepositoryList
            rows={repos}
            setRows={setRepos}
            sets={live.settings?.repository_sets || []}
            projects={projects}
          />
          <WorkerBlock
            backend={backend}
            setBackend={setBackend}
            model={model}
            setModel={setModel}
            mode={mode}
            setMode={setMode}
            workModes={live.settings?.work_modes}
            fallbackBackend={live.settings?.agent_backend || 'opencode'}
            fallbackModel={live.settings?.default_model || ''}
            setModelsLoading={setModelsLoading}
          />
          <ScheduleWhenField value={when} onChange={setWhen} />
          <p className="actions">
            <button type="submit" className="go" disabled={busy || modelsLoading}>
              {busy ? (
                <>
                  <Spinner /> Scheduling…
                </>
              ) : modelsLoading ? (
                <>
                  <Spinner /> Loading models…
                </>
              ) : (
                'Schedule'
              )}
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              disabled={busy || modelsLoading}
              onClick={(e) => void submit(e, true)}
            >
              Run now
            </button>
          </p>
        </>
      )}
      {err && <p className="err">{err}</p>}
    </form>
  )
}

function featureBranchForKey(issueKey: string): string {
  const raw = (issueKey || '').trim() || 'issue'
  const safe = raw.replace(/[^A-Za-z0-9-]/g, '-').replace(/^-+|-+$/g, '') || 'issue'
  return `feature/${safe}`
}

function CreateNew({ onDone }: { onDone: () => void }) {
  const { mode: modeParam = '', tracker: trackerParam = '' } = useParams()
  const navigate = useNavigate()
  const tracker = parseSchedulePath(modeParam, trackerParam)?.tracker ?? 'jira'
  const live = useLive()
  const collections = azureCollectionsFromSettings(live.settings)
  const [collection, setCollection] = useState('')
  const [azureProject, setAzureProject] = useState('')
  const [azureProjects, setAzureProjects] = useState<string[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [repos, setRepos] = useState<RepoRow[]>([emptyRepoRow()])
  const [rememberRepo, setRememberRepo] = useState(false)
  const [projects, setProjects] = useState<ProjectRepository[]>(
    live.settings?.project_repositories || [],
  )
  const [mode, setMode] = useState('build')
  const [model, setModel] = useState('')
  const [backend, setBackend] = useState('')
  const [issueType, setIssueType] = useState('Task')
  const [types, setTypes] = useState<JiraIssueType[]>([])
  const [when, setWhen] = useState(defaultWhen)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(true)

  useEffect(() => {
    if (collection || !collections.length) return
    setCollection(collections[0] || '')
  }, [collections, collection])

  useEffect(() => {
    if (tracker !== 'azure' || !collection.trim()) {
      setAzureProjects([])
      return
    }
    const ac = new AbortController()
    void fetchAzureProjects(collection.trim(), ac.signal)
      .then((p) => {
        if (ac.signal.aborted) return
        const names = p.projects || []
        setAzureProjects(names)
        setAzureProject((cur) => cur || names[0] || '')
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted || isAbortError(e)) return
        setAzureProjects([])
      })
    return () => ac.abort()
  }, [tracker, collection])

  useEffect(() => {
    const ac = new AbortController()
    void fetchIssueTypes(undefined, ac.signal)
      .then((p) => {
        if (ac.signal.aborted) return
        setTypes(p.issue_types || [])
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted || isAbortError(e)) return
      })
    return () => ac.abort()
  }, [])

  useEffect(() => {
    const rows = live.settings?.project_repositories
    if (!rows) return
    // Saved projects stay in the Add repository picker. The list starts empty.
    setProjects(rows)
  }, [live.settings])

  const selectable = useMemo(() => types.filter((t) => !t.subtask), [types])

  const submit = async (e: FormEvent, dispatchNow = false) => {
    e.preventDefault()
    if (modelsLoading) return
    setBusy(true)
    setErr(null)
    try {
      const picked = scheduleRepositoryFields(repos)
      if (!picked.repository_url) {
        setErr('Add a repository')
        setBusy(false)
        return
      }
      await createSchedule({
        title: title.trim(),
        description: description.trim(),
        repository_url: picked.repository_url,
        repository_refs: picked.repository_refs,
        source_branch: picked.source_branch,
        source_branch_mode: picked.source_branch_mode,
        target_branch: picked.target_branch,
        mode,
        issue_type: issueType.trim(),
        scheduled_at: scheduledAtForSubmit(when, dispatchNow),
        dispatch_now: dispatchNow,
        model: model.trim() || undefined,
        backend: backend.trim() || undefined,
        collection_url: tracker === 'azure' ? collection.trim() : undefined,
        azure_project: tracker === 'azure' ? azureProject.trim() : undefined,
      })
      try {
        window.localStorage.setItem(LAST_REPO_KEY, picked.repository_url)
      } catch {
        /* ignore quota / private mode */
      }
      const first = repos.find((row) => row.url.trim())
      if (rememberRepo && first && !projects.some((p) => p.url === first.url.trim())) {
        const row = {
          label: '',
          url: first.url.trim(),
          target_branch: first.target.trim(),
          source_branch: first.sourceMode === 'custom' ? first.source.trim() : '',
        }
        try {
          if (Array.isArray(live.settings?.project_repositories)) {
            const next = [...live.settings.project_repositories, row]
            await patchSettings({ project_repositories: next })
            setProjects(next)
            live.setSettings({ ...live.settings, project_repositories: next })
          } else {
            await patchSettings({ project_repositories_append: [row] })
            setProjects((cur) => [...cur, row])
          }
        } catch {
          /* schedule already created; remember is best-effort */
        }
      }
      setTitle('')
      setDescription('')
      setRememberRepo(false)
      setRepos([emptyRepoRow()])
      onDone()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)}>
      <div className="vd-seg mb-3">
        <button
          type="button"
          className={`vd-seg-btn ${tracker === 'jira' ? 'is-on' : ''}`}
          aria-pressed={tracker === 'jira'}
          onClick={() => navigate(schedulePath('new', 'jira'))}
        >
          Jira
        </button>
        <button
          type="button"
          className={`vd-seg-btn ${tracker === 'azure' ? 'is-on' : ''}`}
          aria-pressed={tracker === 'azure'}
          onClick={() => navigate(schedulePath('new', 'azure'))}
        >
          Azure work item
        </button>
      </div>
      {tracker === 'azure' ? (
        <>
          <label className="field">
            <span>Collection</span>
            {collections.length > 0 ? (
              <select
                value={collection}
                onChange={(e) => {
                  setCollection(e.target.value)
                  setAzureProject('')
                }}
              >
                {collections.map((url) => (
                  <option key={url} value={url}>
                    {url}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={collection}
                onChange={(e) => setCollection(e.target.value)}
                placeholder="https://tfs.example.com/tfs/DefaultCollection"
                required
              />
            )}
          </label>
          <label className="field">
            <span>Team project</span>
            {azureProjects.length > 0 ? (
              <select
                value={azureProject}
                onChange={(e) => setAzureProject(e.target.value)}
                required
              >
                {azureProjects.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={azureProject}
                onChange={(e) => setAzureProject(e.target.value)}
                placeholder="Demo"
                required
              />
            )}
          </label>
        </>
      ) : null}
      <label className="field">
        <span>Title</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} required />
      </label>
      <label className="field">
        <span>Description</span>
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <RepositoryList
        rows={repos}
        setRows={setRepos}
        sets={live.settings?.repository_sets || []}
        projects={projects}
        showRemember
        rememberRepo={rememberRepo}
        setRememberRepo={setRememberRepo}
      />
      <label className="field">
        <span>{tracker === 'azure' ? 'Work item type' : 'Issue type'}</span>
        {tracker === 'azure' ? (
          <select value={issueType} onChange={(e) => setIssueType(e.target.value)}>
            {['Task', 'User Story', 'Bug'].map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        ) : selectable.length ? (
          <select value={issueType} onChange={(e) => setIssueType(e.target.value)}>
            {selectable.map((t) => (
              <option key={t.id || t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
        ) : (
          <input value={issueType} onChange={(e) => setIssueType(e.target.value)} />
        )}
      </label>
      <WorkerBlock
        backend={backend}
        setBackend={setBackend}
        model={model}
        setModel={setModel}
        mode={mode}
        setMode={setMode}
        workModes={live.settings?.work_modes}
        fallbackBackend={live.settings?.agent_backend || 'opencode'}
        fallbackModel={live.settings?.default_model || ''}
        setModelsLoading={setModelsLoading}
      />
      <ScheduleWhenField value={when} onChange={setWhen} />
      {err && <p className="err">{err}</p>}
      <p className="actions">
        <button type="submit" className="go" disabled={busy || modelsLoading}>
          {busy ? (
            <>
              <Spinner /> Creating…
            </>
          ) : modelsLoading ? (
            <>
              <Spinner /> Loading models…
            </>
          ) : (
            'Create schedule'
          )}
        </button>
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={busy || modelsLoading}
          onClick={(e) => void submit(e, true)}
        >
          Create & run now
        </button>
      </p>
    </form>
  )
}

function ScheduleWhenField({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const { date, time } = splitDatetimeLocal(value)
  const [timeDraft, setTimeDraft] = useState(time)
  useEffect(() => {
    if (time) setTimeDraft(time)
  }, [time])
  return (
    <label className="field">
      <span>Run at (24-hour)</span>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="date"
          value={date}
          onChange={(e) =>
            onChange(joinDatetimeLocal(e.target.value, timeDraft || time || '00:00'))
          }
        />
        <input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          spellCheck={false}
          placeholder="14:30"
          pattern="(?:[01]\d|2[0-3]):[0-5]\d"
          title="24-hour time, HH:mm"
          value={timeDraft}
          onChange={(e) => {
            const next = e.target.value.replace(/[^\d:]/g, '').slice(0, 5)
            setTimeDraft(next)
            if (date && joinDatetimeLocal(date, next)) {
              onChange(joinDatetimeLocal(date, next))
            }
          }}
        />
      </div>
      <span className="mt-1 block text-xs text-text-muted">
        Time is 24-hour, for example 14:30 — not am/pm.
      </span>
    </label>
  )
}

function WorkerBlock({
  backend,
  setBackend,
  model,
  setModel,
  mode,
  setMode,
  workModes,
  fallbackBackend,
  fallbackModel,
  setModelsLoading,
}: {
  backend: string
  setBackend: (value: string) => void
  model: string
  setModel: (value: string) => void
  mode?: string
  setMode?: (value: string) => void
  workModes?: WorkMode[]
  fallbackBackend: string
  fallbackModel: string
  setModelsLoading: (loading: boolean) => void
}) {
  return (
    <div className="rounded-xl border border-border p-3">
      <div className="text-sm font-semibold text-text">Worker</div>
      <p className="mt-1 text-xs text-text-muted">
        OpenCode, Codex, or Claude Code. The model list follows the worker.
        Leave both empty to use Settings.
      </p>
      {mode != null && setMode ? (
        <label className="field">
          <span>Mode</span>
          <select value={mode} onChange={(e) => setMode(e.target.value.trim().toLowerCase())}>
            {scheduleModeNames(workModes, mode).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <BackendField
        value={backend}
        onChange={(v) => {
          setModelsLoading(true)
          setBackend(v)
        }}
        fallback={fallbackBackend}
      />
      <ModelField
        value={model}
        onChange={setModel}
        fallback={fallbackModel}
        backend={backend || fallbackBackend}
        onLoadingChange={setModelsLoading}
      />
    </div>
  )
}

function BackendField({
  value,
  onChange,
  fallback,
}: {
  value: string
  onChange: (v: string) => void
  fallback: string
}) {
  return (
    <label className="field">
      <span>Backend</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">
          Settings default{fallback ? ` (${fallback})` : ''}
        </option>
        <option value="opencode">OpenCode</option>
        <option value="codex">Codex</option>
        <option value="claude">Claude Code</option>
      </select>
      <span className="mt-1 block text-xs text-text-muted">
        Leave default to use the worker from Settings.
      </span>
    </label>
  )
}


