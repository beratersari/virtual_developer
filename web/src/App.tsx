import { useCallback, useEffect, useMemo, useState } from 'react'
import { dashboardWsUrl, fetchDashboard, patchSettings } from './api'
import type { DashboardPayload, SettingsPayload } from './types'

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

  const applyPayload = useCallback((payload: DashboardPayload) => {
    setData(payload)
    setError(null)
  }, [])

  useEffect(() => {
    const id = window.setInterval(() => setLocalClock(new Date()), 1000)
    return () => window.clearInterval(id)
  }, [])

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

    fetchDashboard()
      .then(applyPayload)
      .catch((e: Error) => setError(e.message))

    connect()
    return () => {
      closed = true
      if (retry) window.clearTimeout(retry)
      ws?.close()
    }
  }, [applyPayload])

  useEffect(() => {
    if (data?.settings) {
      setSettingsDraft({
        jira_board_id: data.settings.jira_board_id,
        poll_interval_seconds: data.settings.poll_interval_seconds,
        trigger_labels: data.settings.trigger_labels,
        trigger_on_assignment: data.settings.trigger_on_assignment,
        auto_start_plans: data.settings.auto_start_plans,
        max_concurrent_jobs: data.settings.max_concurrent_jobs,
      })
    }
  }, [data?.settings])

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
      const updated = await patchSettings(settingsDraft)
      setData((prev) =>
        prev
          ? {
              ...prev,
              settings: updated,
            }
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
      onClick={() => setTab(id)}
      className={`rounded-lg px-3 py-2 text-sm font-medium transition ${
        tab === id
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
          {navBtn('tasks', 'Tasks')}
          {navBtn('poll', 'Poll monitor')}
          {navBtn('settings', 'Settings')}
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6">
        {error && (
          <div className="rounded-lg border border-rose-800/50 bg-rose-950/40 px-4 py-3 text-sm text-rose-100">
            {error}
          </div>
        )}

        {!data && !error && (
          <div className="text-sm text-slate-400">Loading dashboard…</div>
        )}

        {data && tab === 'tasks' && (
          <section className="space-y-3">
            <div className="flex items-end justify-between">
              <div>
                <h2 className="text-base font-semibold text-slate-100">Current tasks</h2>
                <p className="text-xs text-slate-500">
                  Agent job state from disk and live process cache ({data.tasks.total} total)
                </p>
              </div>
            </div>
            <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/40 shadow-xl shadow-black/20">
              <table className="min-w-full text-left text-sm">
                <thead className="bg-slate-900/80 text-xs uppercase tracking-wide text-slate-400">
                  <tr>
                    <th className="px-4 py-3 font-medium">Issue</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Progress</th>
                    <th className="px-4 py-3 font-medium">Workflow</th>
                    <th className="px-4 py-3 font-medium">Branch / MR</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {data.tasks.tasks.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-4 py-8 text-center text-slate-500">
                        No tasks in state store yet
                      </td>
                    </tr>
                  )}
                  {data.tasks.tasks.map((t) => (
                    <tr key={t.issue_key} className="hover:bg-slate-800/30">
                      <td className="px-4 py-3">
                        <div className="font-mono text-indigo-300">{t.issue_key}</div>
                        <div className="max-w-md truncate text-slate-300">{t.summary}</div>
                        {t.live && (
                          <span className="mt-1 inline-block text-[10px] font-semibold uppercase tracking-wider text-amber-300">
                            live process
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={t.status} />
                        {t.error_message && (
                          <div className="mt-1 max-w-xs truncate text-xs text-rose-300/90">
                            {t.error_message}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-800">
                            <div
                              className="h-full rounded-full bg-indigo-500"
                              style={{ width: `${Math.min(100, t.progress_percentage)}%` }}
                            />
                          </div>
                          <span className="font-mono text-xs text-slate-400">
                            {t.progress_percentage}%
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-slate-300">{t.workflow_type ?? '—'}</td>
                      <td className="px-4 py-3 text-xs text-slate-400">
                        {t.feature_branch && <div className="font-mono">{t.feature_branch}</div>}
                        {t.merge_request_url ? (
                          <a
                            className="text-indigo-400 hover:underline"
                            href={t.merge_request_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            Merge request
                          </a>
                        ) : (
                          !t.feature_branch && '—'
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {data && tab === 'poll' && (
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
                        Waiting for first poll cycle…
                      </td>
                    </tr>
                  )}
                  {data.poll.issues.map((i) => (
                    <tr
                      key={i.key}
                      className={
                        i.will_process
                          ? 'bg-indigo-950/20 hover:bg-indigo-950/30'
                          : 'hover:bg-slate-800/30'
                      }
                    >
                      <td className="px-4 py-3">
                        <div className="font-mono text-indigo-300">{i.key}</div>
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
              Last poll: {data.poll.last_poll_at ?? '—'} · Cycle {data.poll.cycle}
            </p>
          </section>
        )}

        {data && tab === 'settings' && (
          <section className="max-w-xl space-y-4">
            <div>
              <h2 className="text-base font-semibold text-slate-100">Settings</h2>
              <p className="text-xs text-slate-500">
                Runtime values only. Secrets are never shown. Changes apply until process restart
                (unless also set in .env).
              </p>
            </div>

            <div className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/40 p-5">
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
                  onChange={(e) =>
                    setSettingsDraft((s) => ({ ...s, jira_board_id: e.target.value }))
                  }
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
                  onChange={(e) =>
                    setSettingsDraft((s) => ({
                      ...s,
                      poll_interval_seconds: Number(e.target.value),
                    }))
                  }
                />
              </label>
              <label className="block text-sm">
                <span className="text-slate-400">Trigger labels (comma-separated)</span>
                <input
                  className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100 outline-none focus:border-indigo-500"
                  value={settingsDraft.trigger_labels ?? ''}
                  onChange={(e) =>
                    setSettingsDraft((s) => ({ ...s, trigger_labels: e.target.value }))
                  }
                />
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={Boolean(settingsDraft.trigger_on_assignment)}
                  onChange={(e) =>
                    setSettingsDraft((s) => ({
                      ...s,
                      trigger_on_assignment: e.target.checked,
                    }))
                  }
                />
                Trigger on bot assignment
              </label>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={Boolean(settingsDraft.auto_start_plans)}
                  onChange={(e) =>
                    setSettingsDraft((s) => ({ ...s, auto_start_plans: e.target.checked }))
                  }
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
                  onChange={(e) =>
                    setSettingsDraft((s) => ({
                      ...s,
                      max_concurrent_jobs: Number(e.target.value),
                    }))
                  }
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
                <div>Default branch: {data.settings.default_branch}</div>
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
