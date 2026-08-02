import type { ReactNode } from 'react'
import type { GitDelivery, JobItem, TaskDetail } from '../types'
import { JobsTable } from './JobsTable'
import { StatusBadge } from './StatusBadge'

/** Prefer API git_deliveries; fall back to scanning jobs for older payloads. */
function collectDeliveries(detail: TaskDetail): GitDelivery[] {
  if (detail.git_deliveries && detail.git_deliveries.length > 0) {
    return detail.git_deliveries
  }
  const fromJobs: GitDelivery[] = (detail.jobs ?? [])
    .filter(
      (j: JobItem) =>
        j.merge_request_url || j.commit_sha || j.commit_url || j.feature_branch,
    )
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
  if (fromJobs.length > 0) return fromJobs
  if (
    detail.merge_request_url ||
    detail.feature_branch
  ) {
    return [
      {
        feature_branch: detail.feature_branch,
        merge_request_url: detail.merge_request_url,
      },
    ]
  }
  return []
}

type TaskTab = 'overview' | 'logs'

/**
 * Issue / task page — Jira key lifecycle, live slot, all runs, system logs.
 * Single-run artifacts belong on JobDetail.
 */
export function TaskDetailPage({
  detail,
  loading,
  error,
  stale,
  detailTab,
  setDetailTab,
  backLabel,
  onBack,
  onRefresh,
  onOpenJob,
  onCancel,
  cancelling,
}: {
  detail: TaskDetail | null
  loading: boolean
  error: string | null
  stale: boolean
  detailTab: TaskTab
  setDetailTab: (t: TaskTab) => void
  backLabel: string
  onBack: () => void
  onRefresh: () => void
  onOpenJob: (issueKey: string, jobId: string) => void
  onCancel: () => void
  cancelling: boolean
}) {
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <button type="button" onClick={onBack} className="ops-btn-ghost mb-2 text-sm">
            ← Back to {backLabel}
          </button>

          <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Issue / task
          </div>
          <h2 className="mt-0.5 font-mono text-xl font-semibold tracking-tight text-text">
            {detail?.issue_key ?? (loading ? '…' : '—')}
          </h2>
          <p className="mt-1 text-base text-text">{detail?.summary || '—'}</p>

          {detail && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <StatusBadge status={detail.status} />
              {detail.jira_status && (
                <span className="rounded border border-border bg-bg-elevated px-2 py-0.5 text-[11px] text-text-secondary">
                  Jira: {detail.jira_status}
                </span>
              )}
              {detail.live && (
                <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-warning-text">
                  <span className="h-1.5 w-1.5 rounded-full bg-warning" aria-hidden />
                  agent live
                </span>
              )}
              {detail.jira_live === false && (
                <span className="text-[11px] text-warning-text">
                  Jira live fetch unavailable — local cache
                </span>
              )}
            </div>
          )}
          {detail?.status === 'plan_ready' && (
            <p className="mt-2 text-xs text-text-muted">
              To start build: set <code className="text-text-secondary">Mode: build</code>{' '}
              in the issue description and move the issue back to{' '}
              <strong className="font-medium text-text-secondary">To Do</strong>. There is
              no Start button on the dashboard.
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {detail?.can_cancel && (
            <button
              type="button"
              disabled={cancelling}
              onClick={onCancel}
              className="ops-btn ops-btn-danger"
              title="Cancels in-flight work for this Jira issue"
            >
              {cancelling ? 'Cancelling…' : 'Cancel issue work'}
            </button>
          )}
          <button type="button" onClick={onRefresh} className="ops-btn ops-btn-secondary">
            Refresh
          </button>
        </div>
      </div>

      {detail?.description?.trim() ? (
        <div className="ops-card px-4 py-3">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Live issue description
          </div>
          <p className="whitespace-pre-wrap text-sm text-text-secondary">
            {detail.description}
          </p>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-0 border-b border-border">
        {(
          [
            ['overview', 'Overview'],
            ['logs', 'System logs'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setDetailTab(id)}
            className={`border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
              detailTab === id
                ? 'border-accent text-text'
                : 'border-transparent text-text-muted hover:text-text-secondary'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="ops-card min-h-[50vh] p-5">
        {loading && <p className="text-sm text-text-muted">Loading issue…</p>}
        {error && <p className="text-sm text-danger-text">{error}</p>}
        {stale && !error && (
          <p className="mb-3 text-sm text-warning-text">
            Detail may be stale. Use Refresh.
          </p>
        )}

        {detail && detailTab === 'overview' && (
          <div className="space-y-5 text-sm">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <MetaCard label="Issue status" valueNode={<StatusBadge status={detail.status} />} />
              <MetaCard label="Progress" value={`${detail.progress_percentage}%`} />
              <MetaCard
                label="Workflow"
                value={detail.workflow_type ?? '—'}
              />
              <MetaCard
                label="Current task id (live slot)"
                mono
                value={detail.current_task_id ?? '—'}
              />
              <MetaCard
                label="Current OpenCode session"
                mono
                value={detail.current_opencode_session_id ?? '—'}
              />
              <MetaCard
                label="Current job id"
                mono
                value={detail.current_job_id ?? '—'}
              />
              <MetaCard label="Started" mono value={detail.started_at ?? '—'} />
              <MetaCard label="Completed" mono value={detail.completed_at ?? '—'} />
              <MetaCard
                label="Plan path"
                mono
                value={detail.plan_path ?? '—'}
              />
            </div>

            {detail.error_message && (
              <div>
                <div className="mb-1 text-xs font-medium uppercase tracking-wide text-danger-text">
                  Issue error
                </div>
                <pre className="ops-pre max-h-48 text-danger-text">
                  {detail.error_message}
                </pre>
              </div>
            )}

            {(() => {
              const deliveries = collectDeliveries(detail)
              if (deliveries.length === 0) return null
              return (
                <div className="border-t border-border pt-4">
                  <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">
                    Commits &amp; merge requests ({deliveries.length})
                  </div>
                  <p className="mb-3 text-xs text-text-muted">
                    Every push/MR from each run of this issue (re-triggers keep
                    prior links).
                  </p>
                  <ul className="space-y-3">
                    {deliveries.map((d, i) => (
                      <li
                        key={`${d.job_id || 'd'}-${d.merge_request_url || ''}-${d.commit_sha || ''}-${i}`}
                        className="rounded border border-border bg-bg p-3"
                      >
                        <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
                          {d.job_id && (
                            <button
                              type="button"
                              className="font-mono text-accent-text hover:underline"
                              onClick={() => onOpenJob(detail.issue_key, d.job_id!)}
                              title="Open job"
                            >
                              {d.job_id}
                            </button>
                          )}
                          {d.status && <StatusBadge status={d.status} size="sm" />}
                          {d.created_at && (
                            <span className="font-mono">{d.created_at}</span>
                          )}
                          {d.feature_branch && (
                            <span className="font-mono text-text-secondary">
                              {d.feature_branch}
                            </span>
                          )}
                        </div>
                        <div className="mt-2 space-y-1.5 text-sm">
                          {(d.commit_url || d.commit_sha) && (
                            <div>
                              <span className="text-[10px] uppercase text-text-muted">
                                Commit{' '}
                              </span>
                              {d.commit_url ? (
                                <a
                                  href={d.commit_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="break-all font-mono text-accent-text hover:underline"
                                >
                                  {d.commit_sha
                                    ? d.commit_sha.slice(0, 12)
                                    : d.commit_url}
                                </a>
                              ) : (
                                <span className="font-mono text-text-secondary">
                                  {d.commit_sha}
                                </span>
                              )}
                              {d.commit_subject && (
                                <span className="ml-2 text-text-secondary">
                                  {d.commit_subject}
                                </span>
                              )}
                            </div>
                          )}
                          {d.merge_request_url && (
                            <div>
                              <span className="text-[10px] uppercase text-text-muted">
                                MR{' '}
                              </span>
                              <a
                                href={d.merge_request_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="break-all text-accent-text hover:underline"
                              >
                                {d.merge_request_url}
                              </a>
                            </div>
                          )}
                          {!d.merge_request_url &&
                            !d.commit_url &&
                            !d.commit_sha &&
                            d.feature_branch && (
                              <div className="text-text-muted">
                                Branch only (no commit/MR URL recorded)
                              </div>
                            )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )
            })()}

            {(detail.task_ids?.length ?? 0) > 0 && (
              <div>
                <div className="text-[10px] uppercase text-text-muted">All task ids</div>
                <ul className="mt-0.5 space-y-0.5 font-mono text-[11px] text-text-secondary">
                  {detail.task_ids!.map((tid) => (
                    <li key={tid}>{tid}</li>
                  ))}
                </ul>
              </div>
            )}

            {(detail.opencode_session_ids?.length ?? 0) > 0 && (
              <div>
                <div className="text-[10px] uppercase text-text-muted">
                  All OpenCode sessions
                </div>
                <ul className="mt-0.5 space-y-0.5 font-mono text-[11px] text-text-secondary">
                  {detail.opencode_session_ids!.map((sid) => (
                    <li key={sid} className="break-all">
                      {sid}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {detail.retry_history?.length > 0 && (
              <div>
                <div className="mb-1 text-[10px] uppercase text-text-muted">
                  Retry history
                </div>
                <pre className="ops-pre max-h-40 text-text-muted">
                  {JSON.stringify(detail.retry_history, null, 2)}
                </pre>
              </div>
            )}

            <div className="border-t border-border pt-4">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Jobs for {detail.issue_key} ({detail.jobs?.length ?? 0})
              </div>
              <p className="mb-2 text-xs text-text-muted">
                Open a job row to inspect that run only (prompt, session log, job
                ids).
              </p>
              <JobsTable
                jobs={detail.jobs ?? []}
                density="compact"
                onOpenJob={(key, jobId) => onOpenJob(key, jobId)}
              />
            </div>
          </div>
        )}

        {detail && detailTab === 'logs' && (
          <div className="space-y-2 text-sm">
            <p className="text-xs text-text-muted">
              Daemon log lines that mention{' '}
              <span className="font-mono text-text-secondary">{detail.issue_key}</span>{' '}
              (since process start).
            </p>
            {detail.system_logs.length === 0 && (
              <p className="text-text-muted">No matching system log lines.</p>
            )}
            <div className="max-h-[70vh] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-text-secondary">
              {detail.system_logs.map((line, i) => (
                <div
                  key={`${line.timestamp}-${i}`}
                  className="border-b border-border/50 py-0.5"
                >
                  <span className="text-text-muted">{line.timestamp} </span>
                  {line.message}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

function MetaCard({
  label,
  value,
  valueNode,
  mono,
  className = '',
}: {
  label: string
  value?: string
  valueNode?: ReactNode
  mono?: boolean
  className?: string
}) {
  return (
    <div className={`rounded border border-border bg-bg p-3 ${className}`}>
      <div className="text-[10px] font-medium uppercase tracking-wide text-text-muted">
        {label}
      </div>
      {valueNode ? (
        <div className="mt-1">{valueNode}</div>
      ) : (
        <div
          className={`mt-1 break-all text-text ${mono ? 'font-mono text-xs' : 'text-sm'}`}
        >
          {value}
        </div>
      )}
    </div>
  )
}
