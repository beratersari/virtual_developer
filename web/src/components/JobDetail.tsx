import { useMemo, type ReactNode } from 'react'
import {
  findLogForJobPath,
  findPromptForJobPath,
  pathBasename,
} from '../util/paths'
import type { JobItem, TextArtifact } from '../types'
import { PromptBlock } from './PromptBlock'
import { StatusBadge } from './StatusBadge'

type DetailTab = 'overview' | 'prompt' | 'opencode'

/**
 * Job (single run) page — only fields and artifacts for this job.
 * Issue-level state, other runs, system logs live on TaskDetailPage.
 */
export function JobDetail({
  job,
  artifacts,
  loading,
  error,
  stale,
  detailTab,
  setDetailTab,
  backLabel,
  onBack,
  onRefresh,
  onOpenTask,
}: {
  job: JobItem | null
  /** Prompt/session files from the issue sessions dir — filtered to this job only. */
  artifacts: {
    prompts: TextArtifact[]
    sessionLogs: TextArtifact[]
  }
  loading: boolean
  error: string | null
  stale: boolean
  detailTab: DetailTab
  setDetailTab: (t: DetailTab) => void
  backLabel: string
  onBack: () => void
  onRefresh: () => void
  onOpenTask: (issueKey: string) => void
}) {
  const promptMatch = useMemo(() => {
    const match = findPromptForJobPath(
      artifacts.prompts,
      job?.prompt_path,
      job?.session_log_path,
    )
    return {
      match,
      triedMatch: Boolean(job?.prompt_path || job?.session_log_path),
    }
  }, [artifacts.prompts, job])

  const logMatch = useMemo(() => {
    const match = findLogForJobPath(artifacts.sessionLogs, job?.session_log_path)
    return {
      match,
      triedMatch: Boolean(job?.session_log_path),
    }
  }, [artifacts.sessionLogs, job])

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <button type="button" onClick={onBack} className="ops-btn-ghost mb-2 text-sm">
            ← Back to {backLabel}
          </button>

          <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Job (single run)
          </div>
          <h2 className="mt-0.5 break-all font-mono text-xl font-semibold tracking-tight text-text">
            {job?.job_id ?? (loading ? '…' : '—')}
          </h2>

          {job && (
            <p className="mt-1 text-base text-text">{job.summary || '—'}</p>
          )}

          {job && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <StatusBadge status={job.status} />
              {job.live && (
                <span className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-warning-text">
                  <span className="h-1.5 w-1.5 rounded-full bg-warning" aria-hidden />
                  live
                </span>
              )}
              {job.issue_key && (
                <button
                  type="button"
                  className="font-mono text-sm text-accent-text hover:underline"
                  onClick={() => onOpenTask(job.issue_key)}
                  title="Open issue / task page"
                >
                  {job.issue_key}
                </button>
              )}
              <span className="text-xs text-text-muted">
                {job.workflow_type}
                {job.agent ? ` · ${job.agent}` : ''}
              </span>
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {job?.issue_key && (
            <button
              type="button"
              className="ops-btn ops-btn-secondary"
              onClick={() => onOpenTask(job.issue_key)}
            >
              Open issue
            </button>
          )}
          <button type="button" onClick={onRefresh} className="ops-btn ops-btn-secondary">
            Refresh
          </button>
        </div>
      </div>

      {job?.description?.trim() ? (
        <div className="ops-card px-4 py-3">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Description snapshot (this job)
          </div>
          <p className="whitespace-pre-wrap text-sm text-text-secondary">
            {job.description}
          </p>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-0 border-b border-border">
        {(
          [
            ['overview', 'Overview'],
            ['prompt', 'Prompt'],
            ['opencode', 'OpenCode output'],
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
        {loading && <p className="text-sm text-text-muted">Loading job…</p>}
        {error && <p className="text-sm text-danger-text">{error}</p>}
        {stale && !error && (
          <p className="mb-3 text-sm text-warning-text">
            Detail may be stale. Use Refresh.
          </p>
        )}

        {job && detailTab === 'overview' && (
          <div className="space-y-6 text-sm">
            {/* Primary job identity */}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <MetaCard label="Job id" mono value={job.job_id} />
              <MetaCard label="Status" valueNode={<StatusBadge status={job.status} />} />
              <MetaCard label="Progress" value={`${job.progress_percentage}%`} />
              <MetaCard label="Workflow" value={job.workflow_type || '—'} />
              <MetaCard label="Agent" value={job.agent || '—'} />
              <MetaCard label="Issue" mono value={job.issue_key || '—'} />
              <MetaCard label="Started" mono value={job.started_at ?? '—'} />
              <MetaCard label="Completed" mono value={job.completed_at ?? '—'} />
              {job.error_message && (
                <div className="sm:col-span-2 lg:col-span-3">
                  <div className="mb-1 text-xs font-medium uppercase tracking-wide text-danger-text">
                    Error
                  </div>
                  <pre className="ops-pre max-h-48 text-danger-text">
                    {job.error_message}
                  </pre>
                </div>
              )}
            </div>

            {/* Agent attempt diagnostics — not shown on the jobs list */}
            <div className="border-t border-border pt-4">
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted">
                Agent attempt
              </h3>
              <p className="mb-3 text-xs text-text-muted">
                Process-level ids for this run. A job may retry with a new task id;
                the job id stays the same.
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <MetaCard
                  label="Task id (latest attempt)"
                  mono
                  value={job.task_id ?? '—'}
                />
                <MetaCard
                  label="OpenCode session"
                  mono
                  value={job.opencode_session_id ?? '—'}
                />
                {(job.task_ids?.length ?? 0) > 0 && (
                  <MetaCard
                    label="All task ids (incl. retries)"
                    mono
                    className="sm:col-span-2 lg:col-span-3"
                    value={(job.task_ids && job.task_ids.length
                      ? job.task_ids
                      : job.task_id
                        ? [job.task_id]
                        : []
                    ).join(', ') || '—'}
                  />
                )}
                {(job.opencode_session_ids?.length ?? 0) > 1 && (
                  <MetaCard
                    label="All OpenCode sessions"
                    mono
                    className="sm:col-span-2 lg:col-span-3"
                    value={job.opencode_session_ids!.join(', ')}
                  />
                )}
                <MetaCard
                  label="Session log path"
                  mono
                  className="sm:col-span-2 lg:col-span-3"
                  value={job.session_log_path ?? '—'}
                />
                <MetaCard
                  label="Prompt path"
                  mono
                  className="sm:col-span-2 lg:col-span-3"
                  value={job.prompt_path ?? '—'}
                />
              </div>
            </div>
          </div>
        )}

        {job && detailTab === 'prompt' && (
          <div className="space-y-3 text-sm">
            <p className="text-xs text-text-muted">
              Exact text sent to the agent for this job only.
            </p>
            {promptMatch.match ? (
              <PromptBlock
                highlight
                title={`Prompt · ${job.job_id}${
                  promptMatch.match.truncated ? ' (truncated)' : ''
                }`}
                meta={pathBasename(promptMatch.match.path)}
                body={
                  promptMatch.match.content ||
                  promptMatch.match.error ||
                  '(empty)'
                }
              />
            ) : (
              <div className="ops-alert ops-alert-warning">
                {promptMatch.triedMatch ? (
                  <>
                    Could not load prompt file for this job
                    {job.prompt_path ? (
                      <>
                        {' '}
                        (
                        <span className="font-mono">
                          {pathBasename(job.prompt_path)}
                        </span>
                        )
                      </>
                    ) : null}
                    .
                  </>
                ) : (
                  <>No prompt_path on this job record yet.</>
                )}
              </div>
            )}
          </div>
        )}

        {job && detailTab === 'opencode' && (
          <div className="space-y-3 text-sm">
            <p className="text-xs text-text-muted">
              Session log for this job only.
            </p>
            {logMatch.match ? (
              <PromptBlock
                highlight
                title={`Session log · ${job.job_id}${
                  logMatch.match.truncated ? ' (truncated)' : ''
                }`}
                meta={pathBasename(logMatch.match.path)}
                body={
                  logMatch.match.content || logMatch.match.error || '(empty)'
                }
              />
            ) : (
              <div className="ops-alert ops-alert-warning">
                {logMatch.triedMatch ? (
                  <>
                    Could not load session log
                    {job.session_log_path ? (
                      <>
                        {' '}
                        (
                        <span className="font-mono">
                          {pathBasename(job.session_log_path)}
                        </span>
                        )
                      </>
                    ) : null}
                    .
                  </>
                ) : (
                  <>No session_log_path on this job record yet.</>
                )}
              </div>
            )}
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
