import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  cancelSchedule,
  createSchedule,
  dispatchSchedule,
  fetchIssueTypes,
  fetchSchedules,
  fetchSettings,
  patchSettings,
  previewScheduleIssue,
  scheduleExistingIssue,
} from '../../api/client'
import type {
  JiraIssueType,
  ProjectRepository,
  ScheduleItem,
  SchedulePreview,
} from '../../api/types'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { ModelField } from '../../ui/ModelField'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'
import { StatusBadge } from '../../ui/StatusBadge'
import { datetimeLocalToNaiveIso, localNaiveNowIso } from '../../util/time'

const LAST_REPO_KEY = 'vd.schedule.last_repo_url'
const CUSTOM_REPO = '__custom__'

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
  const [error, setError] = useState<string | null>(null)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'existing' | 'new'>('existing')

  const reload = async () => {
    try {
      const p = await fetchSchedules()
      setRows(p.schedules || [])
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [])
  useEffect(() => {
    void reload()
  }, [live.generation])

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Later"
        title="Scheduled"
        description={
          <>
            Queue a run for a chosen time. Existing issues need a valid{' '}
            <span className="font-mono">{'{params}'}</span> template.
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
      </div>
      {mode === 'existing' ? <Existing onDone={() => void reload()} /> : <CreateNew onDone={() => void reload()} />}
      {error && <p className="text-sm text-danger-text">{error}</p>}
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
            {s.title} · {s.mode}
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
            · {s.scheduled_at} · <StatusBadge status={s.status} size="sm" />
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
            {(s.status === 'scheduled' || s.status === 'error' || s.status === 'dispatching') && (
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
        body="Does not delete the Jira issue."
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

function Existing({ onDone }: { onDone: () => void }) {
  const live = useLive()
  const [key, setKey] = useState('')
  const [preview, setPreview] = useState<SchedulePreview | null>(null)
  const [when, setWhen] = useState(defaultWhen)
  const [model, setModel] = useState('')
  const [backend, setBackend] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [looking, setLooking] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)

  const get = async () => {
    setErr(null)
    setLooking(true)
    try {
      const p = await previewScheduleIssue(key.trim().toUpperCase())
      setPreview(p)
      if (p.ok && p.template_valid) setModelsLoading(true)
      setKey(p.issue_key || key)
      if (p.model) setModel(p.model)
      if (p.backend) setBackend(p.backend)
    } catch (e) {
      setPreview(null)
      setErr(e instanceof Error ? e.message : 'Preview failed')
    } finally {
      setLooking(false)
    }
  }

  const submit = async (e: FormEvent, dispatchNow = false) => {
    e.preventDefault()
    if (!preview?.ok || modelsLoading) return
    setBusy(true)
    setErr(null)
    try {
      await scheduleExistingIssue({
        issue_key: preview.issue_key,
        scheduled_at: scheduledAtForSubmit(when, dispatchNow),
        dispatch_now: dispatchNow,
        model: model.trim() || undefined,
        backend: backend.trim() || undefined,
      })
      setPreview(null)
      setKey('')
      setModel('')
      setBackend('')
      onDone()
    } catch (err2) {
      setErr(err2 instanceof Error ? err2.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)}>
      <label className="field">
        <span>Issue key</span>
        <input value={key} onChange={(e) => setKey(e.target.value.toUpperCase())} />
      </label>
      <p className="actions">
        <button type="button" disabled={looking || !key.trim()} onClick={() => void get()}>
          {looking ? 'Looking up…' : 'Look up'}
        </button>
      </p>
      {preview?.ok && preview.template_valid && (
        <>
          <p className="quiet">
            {preview.issue_key} — {preview.title} · {preview.mode} · {preview.repository_url}
          </p>
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
          <label className="field">
            <span>Run at</span>
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
          </label>
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

function CreateNew({ onDone }: { onDone: () => void }) {
  const live = useLive()
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
  const [mode, setMode] = useState<'plan' | 'build'>('build')
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
  const isCustom = repoPick === CUSTOM_REPO || projects.length === 0

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
      <label className="field">
        <span>Title</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} required />
      </label>
      <label className="field">
        <span>Description</span>
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
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
          <label className="field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <input
              type="checkbox"
              checked={rememberRepo}
              onChange={(e) => setRememberRepo(e.target.checked)}
            />
            <span style={{ margin: 0 }}>Remember this project</span>
          </label>
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
          <option value="issue_key">feature/&lt;new issue key&gt;</option>
          <option value="custom">named branch</option>
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
        <select value={mode} onChange={(e) => setMode(e.target.value === 'plan' ? 'plan' : 'build')}>
          <option value="build">build</option>
          <option value="plan">plan</option>
        </select>
      </label>
      <label className="field">
        <span>Issue type</span>
        {selectable.length ? (
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
      <label className="field">
        <span>Run at</span>
        <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
      </label>
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
      </select>
      <span className="mt-1 block text-xs text-text-muted">
        Same job contract as OpenCode. Leave default to use Settings.
      </span>
    </label>
  )
}


