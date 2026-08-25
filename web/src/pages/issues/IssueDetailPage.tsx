import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { cancelTask, fetchTaskDetail } from '../../api/client'
import type { GitDelivery, TaskDetail } from '../../api/types'
import { peekTask, rememberJob, rememberTask } from '../../app/entityCache'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Spinner } from '../../ui/Spinner'
import { LiveDot } from '../../ui/LiveDot'
import { MetaCard } from '../../ui/MetaCard'
import { StatusBadge } from '../../ui/StatusBadge'
import { Tabs } from '../../ui/Tabs'
import { JobsTable } from '../jobs/JobsTable'

function collectDeliveries(detail: TaskDetail): GitDelivery[] {
  if (detail.git_deliveries && detail.git_deliveries.length > 0) return detail.git_deliveries
  return (detail.jobs ?? [])
    .filter((j) => j.merge_request_url || j.commit_sha || j.commit_url || j.feature_branch)
    .map((j) => ({
      job_id: j.job_id,
      feature_branch: j.feature_branch,
      merge_request_url: j.merge_request_url,
      commit_sha: j.commit_sha,
      commit_subject: j.commit_subject,
      commit_url: j.commit_url,
      created_at: j.completed_at ?? j.started_at,
      status: j.status,
    }))
}

export function IssueDetailPage() {
  const { issueKey = '' } = useParams()
  const navigate = useNavigate()
  const live = useLive()
  const cached = peekTask(issueKey.trim().toUpperCase())
  const [detail, setDetail] = useState<TaskDetail | null>(cached)
  const [loading, setLoading] = useState(!cached)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [tab, setTab] = useState<'overview' | 'logs'>('overview')
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [busy, setBusy] = useState(false)
  const reqId = useRef(0)
  const lastSoft = useRef(0)

  const load = useCallback(
    async (soft = false, live = false) => {
      const key = issueKey.trim().toUpperCase()
      if (!key) return
      const req = ++reqId.current
      const haveRow = Boolean(peekTask(key))
      if (!soft && !haveRow) {
        setLoading(true)
        setError(null)
      }
      try {
        const d = await fetchTaskDetail(key, { live })
        if (req !== reqId.current) return
        rememberTask(d)
        for (const j of d.jobs || []) rememberJob(j)
        setDetail(d)
        setStale(false)
      } catch (e) {
        if (req !== reqId.current) return
        if (soft || haveRow) setStale(true)
        else {
          setDetail(null)
          setError(e instanceof Error ? e.message : 'Failed to load issue')
        }
      } finally {
        if (req === reqId.current) setLoading(false)
      }
    },
    [issueKey],
  )

  useEffect(() => {
    setTab('overview')
    lastSoft.current = Date.now()
    const seed = peekTask(issueKey.trim().toUpperCase())
    if (seed) {
      setDetail(seed)
      setLoading(false)
    } else {
      setDetail(null)
    }
    void load(Boolean(seed))
  }, [issueKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const now = Date.now()
    if (now - lastSoft.current < 4000) return
    lastSoft.current = now
    void load(true)
  }, [live.generation, load])

  const routeKey = issueKey.trim().toUpperCase()
  const onCancel = async () => {
    if (!detail?.issue_key || detail.issue_key !== routeKey) return
    setBusy(true)
    setError(null)
    try {
      await cancelTask(routeKey)
      setConfirmCancel(false)
      await load(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Cancel failed')
    } finally {
      setBusy(false)
    }
  }

  const deliveries = detail ? collectDeliveries(detail) : []

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <Link to="/jobs" className="vd-btn-ghost mb-3 inline-block text-sm">
            ← Jobs
          </Link>
          <h1 className="font-mono text-2xl font-semibold tracking-tight text-text">
            {detail?.issue_key ?? (loading ? '…' : '—')}
          </h1>
          <p className="mt-1 text-lg text-text">{detail?.summary || '—'}</p>
          {detail && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <StatusBadge status={detail.status} />
              {detail.jira_status && (
                <span className="rounded border border-border bg-bg-elevated px-2 py-0.5 text-[11px] text-text-secondary">
                  Jira: {detail.jira_status}
                </span>
              )}
              {detail.live && <LiveDot label="agent live" />}
            </div>
          )}
          {detail?.status === 'plan_ready' && (
            <p className="mt-2 text-xs text-text-muted">
              To start build: keep this issue on To Do and add label{' '}
              <code>ai-start-work</code> or <code>ai-execute</code>. There is no Start button.
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {detail?.can_cancel && detail.issue_key === routeKey && (
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirmCancel(true)}
              className="vd-btn vd-btn-danger"
            >
              Stop work
            </button>
          )}
          <button
            type="button"
            onClick={() => void load(Boolean(detail), true)}
            className="vd-btn vd-btn-secondary"
            disabled={loading}
          >
            {loading ? (
              <>
                <Spinner /> Refreshing…
              </>
            ) : (
              'Refresh'
            )}
          </button>
        </div>
      </div>

      {detail?.description?.trim() ? (
        <div className="vd-card px-4 py-3">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            {detail.jira_live ? 'Live issue description' : 'Issue description'}
          </div>
          <p className="whitespace-pre-wrap text-sm text-text-secondary">{detail.description}</p>
        </div>
      ) : null}

      <Tabs
        tabs={[
          { id: 'overview', label: 'Overview' },
          { id: 'logs', label: 'System logs' },
        ]}
        value={tab}
        onChange={setTab}
      />

      <div className="vd-card min-h-[50vh] p-5">
        {loading && !detail && <p className="text-sm text-text-muted">Loading issue…</p>}
        {error && <p className="text-sm text-danger-text">{error}</p>}
        {stale && !error && (
          <p className="mb-3 text-sm text-warning-text">Detail may be stale. Use Refresh.</p>
        )}

        {detail && tab === 'overview' && (
          <div className="space-y-5 text-sm">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <MetaCard label="Issue status" valueNode={<StatusBadge status={detail.status} />} />
              <MetaCard label="Workflow" value={detail.workflow_type ?? '—'} />
              <MetaCard label="Current job id" mono value={detail.current_job_id ?? '—'} />
              <MetaCard label="Started" mono value={detail.started_at ?? '—'} />
              <MetaCard label="Completed" mono value={detail.completed_at ?? '—'} />
            </div>
            {detail.error_message && (
              <pre className="vd-pre max-h-48 text-danger-text">{detail.error_message}</pre>
            )}
            {deliveries.length > 0 && (
              <ul className="space-y-3 border-t border-border pt-4">
                {deliveries.map((d, i) => (
                  <li
                    key={`${d.job_id || 'd'}-${i}`}
                    className="rounded border border-border bg-bg p-3"
                  >
                    <div className="flex flex-wrap gap-2 text-[11px] text-text-muted">
                      {d.job_id && (
                        <button
                          type="button"
                          className="font-mono text-accent-text hover:underline"
                          onClick={() => navigate(`/jobs/${encodeURIComponent(d.job_id!)}`)}
                        >
                          {d.job_id}
                        </button>
                      )}
                      {d.status && <StatusBadge status={d.status} size="sm" />}
                      {d.feature_branch && (
                        <span className="font-mono">{d.feature_branch}</span>
                      )}
                    </div>
                    {d.merge_request_url && (
                      <a
                        href={d.merge_request_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mt-2 inline-block break-all text-sm text-accent-text hover:underline"
                      >
                        {d.merge_request_url}
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <div className="border-t border-border pt-4">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Jobs for {detail.issue_key}
              </div>
              <JobsTable
                jobs={detail.jobs ?? []}
                compact
                onOpenJob={(_key, jobId) => navigate(`/jobs/${encodeURIComponent(jobId)}`)}
              />
            </div>
          </div>
        )}

        {detail && tab === 'logs' && (
          <div className="max-h-[70vh] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] text-text-secondary">
            {detail.system_logs.length === 0 && <p>No matching system log lines.</p>}
            {detail.system_logs.map((line, i) => (
              <div key={`${line.timestamp}-${i}`}>
                <span className="text-text-muted">{line.timestamp} </span>
                {line.message}
              </div>
            ))}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmCancel}
        title={`Cancel work for ${detail?.issue_key}?`}
        body="Stops in-flight agent work for this Jira issue."
        confirmLabel="Stop work"
        danger
        busy={busy}
        onConfirm={() => void onCancel()}
        onCancel={() => setConfirmCancel(false)}
      />
    </section>
  )
}
