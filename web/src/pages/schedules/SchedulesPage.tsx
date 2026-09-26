import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  cancelSchedule,
  createSchedule,
  dispatchSchedule,
  fetchAzureProjects,
  fetchIssueTypes,
  fetchSchedules,
  fetchSettings,
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
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { ModelField } from '../../ui/ModelField'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'
import { StatusBadge } from '../../ui/StatusBadge'
import {
  datetimeLocalToNaiveIso,
  formatScheduleWhen,
  joinDatetimeLocal,
  localNaiveNowIso,
  splitDatetimeLocal,
} from '../../util/time'

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
  const live = useLive()
  const [rows, setRows] = useState<ScheduleItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(PAGE_SIZE)
  const [error, setError] = useState<string | null>(null)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'existing' | 'new' | 'mr' | 'pr'>('existing')
  const lastGenReload = useRef(0)
  const reqId = useRef(0)

  const reload = useCallback(async (pageOverride?: number) => {
    const nextPage = pageOverride ?? page
    const req = ++reqId.current
    try {
      const p = await fetchSchedules({ page: nextPage, pageSize: PAGE_SIZE })
      if (req !== reqId.current) return
      const size = p.page_size ?? PAGE_SIZE
      const count = p.total ?? 0
      const pages = Math.max(1, Math.ceil(count / size) || 1)
      const landed = p.page ?? nextPage
      setRows(p.schedules || [])
      setTotal(count)
      setPageSize(size)
      setError(null)
      if (landed > pages) {
        setPage(pages)
        return
      }
      if (landed !== nextPage) setPage(landed)
    } catch (e) {
      if (req !== reqId.current) return
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }, [page])

  useEffect(() => {
    void reload()
  }, [reload])
  useEffect(() => {
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload()
  }, [live.generation, reload])

  const currentPage = page
  const size = pageSize || PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (currentPage - 1) * size + 1
  const to = Math.min(currentPage * size, total)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Later"
        title="Scheduled"
        description={
          <>
            Queue a run for a chosen time. Existing MR or PR posts your prompt
            on the merge request / pull request when it fires, then posts the
            agent answer when the worker finishes.
          </>
        }
      />
      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
        <button
          type="button"
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium ${
            mode === 'existing' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setMode('existing')}
        >
          Existing issue
        </button>
        <button
          type="button"
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium ${
            mode === 'new' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setMode('new')}
        >
          New issue
        </button>
        <button
          type="button"
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium ${
            mode === 'mr' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setMode('mr')}
        >
          Existing MR
        </button>
        <button
          type="button"
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium ${
            mode === 'pr' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setMode('pr')}
        >
          Existing PR
        </button>
      </div>
      {mode === 'existing' ? (
        <Existing onDone={() => { setPage(1); void reload(1) }} />
      ) : mode === 'mr' ? (
        <ExistingMr onDone={() => { setPage(1); void reload(1) }} />
      ) : mode === 'pr' ? (
        <ExistingPr onDone={() => { setPage(1); void reload(1) }} />
      ) : (
        <CreateNew onDone={() => { setPage(1); void reload(1) }} />
      )}
      {error && <p className="text-sm text-danger-text">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2 text-xs text-text-muted">
        <span>
          {from}–{to} of {total}
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
      <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
        {rows.map((s) => (
          <li key={s.schedule_id} className="py-3 text-sm">
            {s.issue_key ? (
              <Link className="font-mono text-accent-text hover:underline" to={`/tasks/${encodeURIComponent(s.issue_key)}`}>
                {s.issue_key}
              </Link>
            ) : (
              '—'
            )}{' '}
            {s.source === 'gitlab_mr' && s.mr_iid ? (
              <>
                {s.merge_request_url ? (
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
                )}{' '}
              </>
            ) : null}
            {s.source === 'azure_pr' && s.pr_id ? (
              <>
                {s.merge_request_url ? (
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
                )}{' '}
              </>
            ) : null}
            {s.title} ·{' '}
            {s.source === 'gitlab_mr'
              ? 'mr follow-up'
              : s.source === 'azure_pr'
                ? 'pr follow-up'
                : s.mode}
            {s.backend ? (
              <>
                {' '}
                · <span className="font-mono text-xs text-text-secondary">{s.backend}</span>
              </>
            ) : null}
            {s.model ? (
              <>
                {' '}
                · <span className="font-mono text-xs text-text-secondary">{s.model}</span>
              </>
            ) : null}{' '}
            · {formatScheduleWhen(s.scheduled_at)} · <StatusBadge status={s.status} size="sm" />
            {(s.status === 'scheduled' || s.status === 'error') && (
              <>
                {' '}
                <button
                  type="button"
                  className="vd-btn-ghost text-accent-text"
                  onClick={() => setRunId(s.schedule_id)}
                >
                  run now
                </button>
              </>
            )}
            {(s.status === 'scheduled' || s.status === 'error') && (
              <>
                {' '}
                <button
                  type="button"
                  className="vd-btn-ghost text-danger-text"
                  onClick={() => setCancelId(s.schedule_id)}
                >
                  cancel
                </button>
              </>
            )}
          </li>
        ))}
        {rows.length === 0 && <li className="py-6 text-text-muted">Nothing scheduled.</li>}
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
        <label className="field">
          <span>Project</span>
          <select
            value={repoPick}
            onChange={(e) => {
              const v = e.target.value
              setRepoPick(v)
              setPreview(null)
              if (v === CUSTOM_REPO) {
                setRepo('')
                return
              }
              const hit = projects.find((p) => p.url === v)
              if (hit) setRepo(hit.url)
            }}
          >
            {projects.map((p) => (
              <option key={p.url} value={p.url}>
                {p.label || p.url}
              </option>
            ))}
            <option value={CUSTOM_REPO}>Other URL…</option>
          </select>
        </label>
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
        <button type="button" disabled={looking || !repo.trim() || !iid.trim()} onClick={() => void get()}>
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
          <BackendField
            value={backend}
            onChange={(v) => {
              setModelsLoading(true)
              setBackend(v)
            }}
            fallback={live.settings?.agent_backend || 'opencode'}
          />
          <ModelField
            value={model}
            onChange={setModel}
            fallback={live.settings?.default_model || ''}
            backend={backend || live.settings?.agent_backend || 'opencode'}
            onLoadingChange={setModelsLoading}
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
        <label className="field">
          <span>Project</span>
          <select
            value={repoPick}
            onChange={(e) => {
              const v = e.target.value
              setRepoPick(v)
              setPreview(null)
              if (v === CUSTOM_REPO) {
                setRepo('')
                return
              }
              const hit = projects.find((p) => p.url === v)
              if (hit) setRepo(hit.url)
            }}
          >
            {projects.map((p) => (
              <option key={p.url} value={p.url}>
                {p.label || p.url}
              </option>
            ))}
            <option value={CUSTOM_REPO}>Other URL…</option>
          </select>
        </label>
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
        <button type="button" disabled={looking || !repo.trim() || !iid.trim()} onClick={() => void get()}>
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
          <BackendField
            value={backend}
            onChange={(v) => {
              setModelsLoading(true)
              setBackend(v)
            }}
            fallback={live.settings?.agent_backend || 'opencode'}
          />
          <ModelField
            value={model}
            onChange={setModel}
            fallback={live.settings?.default_model || ''}
            backend={backend || live.settings?.agent_backend || 'opencode'}
            onLoadingChange={setModelsLoading}
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
  const live = useLive()
  const [tracker, setTracker] = useState<'jira' | 'azure'>('jira')
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
  const [repo, setRepo] = useState('')
  const [repoPick, setRepoPick] = useState(CUSTOM_REPO)
  const [srcMode, setSrcMode] = useState<'issue_key' | 'custom'>('issue_key')
  const [source, setSource] = useState('develop')
  const [target, setTarget] = useState('develop')
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
    if (fromIssue && !preferred) {
      setRepoPick(CUSTOM_REPO)
      setRepo(fromIssue)
    } else if (preferred) {
      setRepoPick(preferred.url)
      applyProject(preferred, setRepo, setTarget, setSource)
      if (fromIssue) setRepo(fromIssue)
    } else {
      setRepoPick(CUSTOM_REPO)
      setRepo(fromIssue)
    }
    const lookedUpSource = (p.source_branch || '').trim()
    const feature = featureBranchForKey(p.issue_key || '')
    if (lookedUpSource && lookedUpSource.toLowerCase() !== feature.toLowerCase()) {
      setSrcMode('custom')
      setSource(lookedUpSource)
    } else {
      setSrcMode('issue_key')
      if (lookedUpSource) setSource(lookedUpSource)
    }
    if (p.target_branch) setTarget(p.target_branch)
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
    if (!repo.trim()) {
      setErr('Pick a project or enter a repository URL')
      return
    }
    if (srcMode === 'custom' && !source.trim()) {
      setErr('Enter a source branch')
      return
    }
    if (!target.trim()) {
      setErr('Enter a target branch')
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
        repository_url: repo.trim(),
        source_branch: srcMode === 'custom' ? source.trim() : undefined,
        target_branch: target.trim(),
        mode,
        source_branch_mode: srcMode,
      })
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
      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1 mb-3">
        <button
          type="button"
          className={`rounded-full px-3 py-1 text-sm font-medium ${
            tracker === 'jira' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => {
            setTracker('jira')
            setPreview(null)
          }}
        >
          Jira
        </button>
        <button
          type="button"
          className={`rounded-full px-3 py-1 text-sm font-medium ${
            tracker === 'azure' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => {
            setTracker('azure')
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
        <button type="button" disabled={looking || !canLookUp} onClick={() => void get()}>
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
          <ProjectBranchFields
              projects={projects}
              repo={repo}
              setRepo={setRepo}
              repoPick={repoPick}
              setRepoPick={setRepoPick}
              srcMode={srcMode}
              setSrcMode={setSrcMode}
              source={source}
              setSource={setSource}
              target={target}
              setTarget={setTarget}
              mode={mode}
              setMode={setMode}
              showRemember={false}
              rememberRepo={false}
              setRememberRepo={() => undefined}
            />
          <BackendField
            value={backend}
            onChange={(v) => {
              setModelsLoading(true)
              setBackend(v)
            }}
            fallback={live.settings?.agent_backend || 'opencode'}
          />
          <ModelField
            value={model}
            onChange={setModel}
            fallback={live.settings?.default_model || ''}
            backend={backend || live.settings?.agent_backend || 'opencode'}
            onLoadingChange={setModelsLoading}
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

function applyProject(
  p: ProjectRepository,
  setRepo: (v: string) => void,
  setTarget: (v: string) => void,
  setSource: (v: string) => void,
) {
  setRepo(p.url)
  if (p.target_branch) setTarget(p.target_branch)
  if (p.source_branch) setSource(p.source_branch)
}

function ProjectBranchFields({
  projects,
  repo,
  setRepo,
  repoPick,
  setRepoPick,
  srcMode,
  setSrcMode,
  source,
  setSource,
  target,
  setTarget,
  mode,
  setMode,
  showRemember,
  rememberRepo,
  setRememberRepo,
}: {
  projects: ProjectRepository[]
  repo: string
  setRepo: (v: string) => void
  repoPick: string
  setRepoPick: (v: string) => void
  srcMode: 'issue_key' | 'custom'
  setSrcMode: (v: 'issue_key' | 'custom') => void
  source: string
  setSource: (v: string) => void
  target: string
  setTarget: (v: string) => void
  mode: string
  setMode: (v: string) => void
  showRemember: boolean
  rememberRepo: boolean
  setRememberRepo: (v: boolean) => void
}) {
  const isCustom = repoPick === CUSTOM_REPO || projects.length === 0
  const live = useLive()
  const modeNames = scheduleModeNames(live.settings?.work_modes, mode)
  return (
    <>
      {projects.length > 0 && (
        <label className="field">
          <span>Project</span>
          <select
            value={repoPick}
            onChange={(e) => {
              const v = e.target.value
              setRepoPick(v)
              if (v === CUSTOM_REPO) {
                setRepo('')
                return
              }
              const hit = projects.find((p) => p.url === v)
              if (hit) applyProject(hit, setRepo, setTarget, setSource)
            }}
          >
            {projects.map((p) => (
              <option key={p.url} value={p.url}>
                {p.label || p.url}
              </option>
            ))}
            <option value={CUSTOM_REPO}>Other URL…</option>
          </select>
        </label>
      )}
      {(isCustom || projects.length === 0) && (
        <>
          <label className="field">
            <span>Repository</span>
            <input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="https://gitlab.com/group/repo.git"
              required
            />
          </label>
          {showRemember && (
            <label className="field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <input
                type="checkbox"
                checked={rememberRepo}
                onChange={(e) => setRememberRepo(e.target.checked)}
              />
              <span style={{ margin: 0 }}>Remember this project</span>
            </label>
          )}
        </>
      )}
      {!isCustom && repo ? (
        <p className="quiet font-mono text-xs">{repo}</p>
      ) : null}
      <p className="quiet text-xs">
        Saved remotes live in{' '}
        <Link to="/settings" className="text-accent-text hover:underline">
          Settings → Projects
        </Link>
        .
      </p>
      <label className="field">
        <span>Source</span>
        <select value={srcMode} onChange={(e) => setSrcMode(e.target.value === 'custom' ? 'custom' : 'issue_key')}>
          <option value="issue_key">feature/&lt;issue key&gt;</option>
          <option value="custom">custom branch</option>
        </select>
      </label>
      {srcMode === 'custom' && (
        <label className="field">
          <span>Branch</span>
          <input value={source} onChange={(e) => setSource(e.target.value)} required />
        </label>
      )}
      <label className="field">
        <span>Target</span>
        <input value={target} onChange={(e) => setTarget(e.target.value)} required />
      </label>
      <label className="field">
        <span>Mode</span>
        <select value={mode} onChange={(e) => setMode(e.target.value.trim().toLowerCase())}>
          {modeNames.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
    </>
  )
}

function CreateNew({ onDone }: { onDone: () => void }) {
  const live = useLive()
  const [tracker, setTracker] = useState<'jira' | 'azure'>('jira')
  const collections = azureCollectionsFromSettings(live.settings)
  const [collection, setCollection] = useState('')
  const [azureProject, setAzureProject] = useState('')
  const [azureProjects, setAzureProjects] = useState<string[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [repo, setRepo] = useState('')
  const [repoPick, setRepoPick] = useState(CUSTOM_REPO)
  const [rememberRepo, setRememberRepo] = useState(false)
  const [projects, setProjects] = useState<ProjectRepository[]>(
    live.settings?.project_repositories || [],
  )
  const [srcMode, setSrcMode] = useState<'issue_key' | 'custom'>('issue_key')
  const [source, setSource] = useState('develop')
  const [target, setTarget] = useState('develop')
  const [mode, setMode] = useState('build')
  const [model, setModel] = useState('')
  const [backend, setBackend] = useState('')
  const [issueType, setIssueType] = useState('Task')
  const [types, setTypes] = useState<JiraIssueType[]>([])
  const [when, setWhen] = useState(defaultWhen)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(true)
  const seeded = useRef(false)

  useEffect(() => {
    if (collection || !collections.length) return
    setCollection(collections[0] || '')
  }, [collections, collection])

  useEffect(() => {
    if (tracker !== 'azure' || !collection.trim()) {
      setAzureProjects([])
      return
    }
    void fetchAzureProjects(collection.trim())
      .then((p) => {
        const names = p.projects || []
        setAzureProjects(names)
        setAzureProject((cur) => cur || names[0] || '')
      })
      .catch(() => setAzureProjects([]))
  }, [tracker, collection])

  useEffect(() => {
    void fetchIssueTypes()
      .then((p) => setTypes(p.issue_types || []))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const rows = live.settings?.project_repositories
    if (rows) setProjects(rows)
  }, [live.settings])

  useEffect(() => {
    void fetchSettings()
      .then((s) => {
        const rows = s.project_repositories || []
        setProjects(rows)
        if (seeded.current) return
        seeded.current = true
        const last = (() => {
          try {
            return window.localStorage.getItem(LAST_REPO_KEY) || ''
          } catch {
            return ''
          }
        })()
        const preferred =
          rows.find((p) => p.url === last) || (rows.length === 1 ? rows[0] : null)
        if (preferred) {
          setRepoPick(preferred.url)
          applyProject(preferred, setRepo, setTarget, setSource)
        }
      })
      .catch(() => undefined)
  }, [])

  const selectable = useMemo(() => types.filter((t) => !t.subtask), [types])

  const submit = async (e: FormEvent, dispatchNow = false) => {
    e.preventDefault()
    if (modelsLoading) return
    setBusy(true)
    setErr(null)
    try {
      const url = repo.trim()
      await createSchedule({
        title: title.trim(),
        description: description.trim(),
        repository_url: url,
        source_branch: srcMode === 'custom' ? source.trim() : undefined,
        source_branch_mode: srcMode,
        target_branch: target.trim(),
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
        window.localStorage.setItem(LAST_REPO_KEY, url)
      } catch {
        /* ignore quota / private mode */
      }
      if (rememberRepo && url && !projects.some((p) => p.url === url)) {
        const next = [
          ...projects,
          {
            label: '',
            url,
            target_branch: target.trim(),
            source_branch: srcMode === 'custom' ? source.trim() : '',
          },
        ]
        try {
          const updated = await patchSettings({ project_repositories: next })
          setProjects(updated.project_repositories || next)
        } catch {
          /* schedule already created; remember is best-effort */
        }
      }
      setTitle('')
      setDescription('')
      setRememberRepo(false)
      onDone()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)}>
      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1 mb-3">
        <button
          type="button"
          className={`rounded-full px-3 py-1 text-sm font-medium ${
            tracker === 'jira' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setTracker('jira')}
        >
          Jira
        </button>
        <button
          type="button"
          className={`rounded-full px-3 py-1 text-sm font-medium ${
            tracker === 'azure' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setTracker('azure')}
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
      <ProjectBranchFields
        projects={projects}
        repo={repo}
        setRepo={setRepo}
        repoPick={repoPick}
        setRepoPick={setRepoPick}
        srcMode={srcMode}
        setSrcMode={setSrcMode}
        source={source}
        setSource={setSource}
        target={target}
        setTarget={setTarget}
        mode={mode}
        setMode={setMode}
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
      <BackendField
        value={backend}
        onChange={(v) => {
          setModelsLoading(true)
          setBackend(v)
        }}
        fallback={live.settings?.agent_backend || 'opencode'}
      />
      <ModelField
        value={model}
        onChange={setModel}
        fallback={live.settings?.default_model || ''}
        backend={backend || live.settings?.agent_backend || 'opencode'}
        onLoadingChange={setModelsLoading}
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


