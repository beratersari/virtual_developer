import type { JobItem } from '../../api/types'
import { jobChannelLabel } from '../../util/jobChannel'
import { sortJobsByCreatedAt } from '../../util/jobs'
import { jobIsDeletable, statusToneClass } from '../../util/status'
import { resolveJobWorker, workerLabel, type WorkerId } from '../../util/worker'
import { LiveDot } from '../../ui/LiveDot'
import { StatusBadge } from '../../ui/StatusBadge'

function usualWorker(fallback: string): WorkerId {
  const name = fallback.trim().toLowerCase()
  if (name === 'codex' || name === 'openai' || name === 'openai-codex') return 'codex'
  if (name === 'claude' || name === 'claude-code' || name === 'anthropic') return 'claude'
  return 'opencode'
}

export function JobsTable({
  jobs,
  compact = false,
  selectable = false,
  selectedIds,
  onToggleSelect,
  onOpenJob,
  empty = 'Nothing here for this filter.',
  fallbackWorker = '',
}: {
  jobs: JobItem[]
  compact?: boolean
  selectable?: boolean
  selectedIds?: Set<string>
  onToggleSelect?: (jobId: string) => void
  onOpenJob: (issueKey: string, jobId: string) => void
  empty?: string
  fallbackWorker?: string
}) {
  if (jobs.length === 0) {
    return (
      <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
        {empty}
      </div>
    )
  }

  const ordered = sortJobsByCreatedAt(jobs)

  return (
    <div className={compact ? 'space-y-2' : 'space-y-2.5'}>
      {ordered.map((j) => {
        const canSelect = jobIsDeletable(j.status, Boolean(j.live))
        const isChecked = Boolean(selectedIds?.has(j.job_id))
        const channel = jobChannelLabel(j)
        const worker = resolveJobWorker(j, fallbackWorker)
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
                {!channel && (j.source || 'jira') === 'gitlab' && (
                  <span className="vd-tag">
                    GitLab
                  </span>
                )}
                {!channel &&
                  ((j.source || 'jira') === 'azure' ||
                    (j.source || 'jira') === 'azure_workitem') && (
                  <span className="vd-tag">
                    Azure
                  </span>
                )}
                {j.live && <LiveDot />}
                <StatusBadge status={j.status} size="sm" />
                {channel && (
                  <span className="vd-tag">
                    {channel}
                  </span>
                )}
                {worker !== usualWorker(fallbackWorker) && (
                  <span className="vd-tag">{workerLabel(worker)}</span>
                )}
              </div>
              <div className={`mt-1 truncate text-text ${compact ? 'text-sm' : 'text-[15px]'}`}>
                {j.summary || 'Untitled run'}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-text-muted">
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
