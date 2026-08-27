import type { JobItem } from '../../api/types'
import { sortJobsByCreatedAt } from '../../util/jobs'
import { jobIsDeletable, statusToneClass } from '../../util/status'
import { resolveJobWorker, workerLabel } from '../../util/worker'
import { LiveDot } from '../../ui/LiveDot'
import { StatusBadge } from '../../ui/StatusBadge'

export function JobsTable({
  jobs,
  compact = false,
  selectable = false,
  selectedIds,
  onToggleSelect,
  onOpenJob,
}: {
  jobs: JobItem[]
  compact?: boolean
  selectable?: boolean
  selectedIds?: Set<string>
  onToggleSelect?: (jobId: string) => void
  onOpenJob: (issueKey: string, jobId: string) => void
}) {
  if (jobs.length === 0) {
    return (
      <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
        Nothing here for this filter.
      </div>
    )
  }

  const ordered = sortJobsByCreatedAt(jobs)

  return (
    <div className={compact ? 'space-y-2' : 'space-y-2.5'}>
      {ordered.map((j) => {
        const canSelect = jobIsDeletable(j.status, Boolean(j.live))
        const isChecked = Boolean(selectedIds?.has(j.job_id))
        return (
          <div
            key={j.job_id}
            className={`vd-job ${isChecked ? 'ring-1 ring-accent/70' : ''}`}
            role="button"
            tabIndex={0}
            onClick={() => onOpenJob(j.issue_key, j.job_id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onOpenJob(j.issue_key, j.job_id)
              }
            }}
          >
            <div className={`vd-job-bar ${statusToneClass(j.status)}`} />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm font-semibold text-text">
                  {j.issue_key}
                </span>
                {(j.source || 'jira') === 'gitlab' && (
                  <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
                    GitLab
                  </span>
                )}
                {j.live && <LiveDot />}
                <StatusBadge status={j.status} size="sm" />
                <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
                  {workerLabel(resolveJobWorker(j))}
                </span>
              </div>
              <div className={`mt-1 truncate text-text ${compact ? 'text-sm' : 'text-[15px]'}`}>
                {j.summary || 'Untitled run'}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-text-muted">
                <span>{j.job_id.length > 22 ? `${j.job_id.slice(0, 20)}…` : j.job_id}</span>
                {j.workflow_type && <span>{j.workflow_type}</span>}
                {j.agent && <span>{j.agent}</span>}
                <span>{j.started_at ?? 'not started'}</span>
                {j.merge_request_url && (
                  <a
                    href={j.merge_request_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-accent-text hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    Merge request
                  </a>
                )}
                {j.delivery_status === 'no_new_commits' && <span>no new commits</span>}
              </div>
              {j.error_message && (
                <div className="mt-1.5 truncate text-xs text-danger-text">{j.error_message}</div>
              )}
            </div>
            {selectable && (
              <div onClick={(e) => e.stopPropagation()} className="pt-1">
                <input
                  type="checkbox"
                  className="vd-checkbox"
                  aria-label={canSelect ? `Select ${j.job_id}` : `Cannot delete live job`}
                  checked={isChecked}
                  disabled={!canSelect}
                  onChange={() => {
                    if (canSelect) onToggleSelect?.(j.job_id)
                  }}
                />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
