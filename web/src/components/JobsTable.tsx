import type { JobItem } from '../types'
import { jobIsDeletable } from '../util/status'
import { StatusBadge } from './StatusBadge'

export function JobsTable({
  jobs,
  onOpenJob,
  onOpenIssue,
  compact = false,
  selectedJobId = null,
  density = 'comfortable',
  showProgress = true,
  selectable = false,
  selectedIds,
  onToggleSelect,
  onToggleSelectAll,
}: {
  jobs: JobItem[]
  /** Open the job (single run) page. */
  onOpenJob: (issueKey: string, jobId: string) => void
  /** Optional: open issue/task page when clicking the issue key. */
  onOpenIssue?: (issueKey: string) => void
  compact?: boolean
  selectedJobId?: string | null
  density?: 'comfortable' | 'compact'
  /** When false, hide progress column (list can still show for active jobs only). */
  showProgress?: boolean
  /** When true, show checkboxes for multi-select delete. */
  selectable?: boolean
  selectedIds?: Set<string>
  onToggleSelect?: (jobId: string) => void
  onToggleSelectAll?: () => void
}) {
  const dense = density === 'compact' || compact
  // [Select] | Job | Issue | Status | [Workflow] | Started | [Progress]
  const colCount =
    (selectable ? 1 : 0) + (compact ? 4 : 5) + (!compact && showProgress ? 1 : 0)

  const deletableIds = jobs
    .filter((j) => jobIsDeletable(j.status, Boolean(j.live)))
    .map((j) => j.job_id)
  const allDeletableSelected =
    deletableIds.length > 0 &&
    deletableIds.every((id) => selectedIds?.has(id))
  const someSelected = Boolean(
    selectedIds && deletableIds.some((id) => selectedIds.has(id)),
  )

  return (
    <div className="ops-table-wrap">
      <table className={`ops-table ${dense ? 'ops-table-compact' : ''}`}>
        <thead>
          <tr>
            {selectable && (
              <th className="w-10">
                <input
                  type="checkbox"
                  className="ops-checkbox"
                  aria-label="Select all deletable jobs on this page"
                  checked={allDeletableSelected}
                  ref={(el) => {
                    if (el) {
                      el.indeterminate = someSelected && !allDeletableSelected
                    }
                  }}
                  disabled={deletableIds.length === 0}
                  onChange={() => onToggleSelectAll?.()}
                />
              </th>
            )}
            <th>Job</th>
            <th>Issue</th>
            <th>Status</th>
            {!compact && <th>Workflow</th>}
            <th>Started</th>
            {!compact && showProgress && <th>Progress</th>}
          </tr>
        </thead>
        <tbody>
          {jobs.length === 0 && (
            <tr>
              <td colSpan={colCount} className="px-4 py-8 text-center text-text-muted">
                No jobs match this filter.
              </td>
            </tr>
          )}
          {jobs.map((j) => {
            const terminal = ['completed', 'error', 'cancelled', 'superseded'].includes(
              (j.status || '').toLowerCase(),
            )
            const showBar = showProgress && !compact && (!terminal || j.progress_percentage < 100)
            const canSelect = jobIsDeletable(j.status, Boolean(j.live))
            const isChecked = Boolean(selectedIds?.has(j.job_id))
            const rowSelected =
              selectedJobId === j.job_id || (selectable && isChecked)
            return (
              <tr
                key={j.job_id}
                className={rowSelected ? 'ops-row-selected' : ''}
              >
                {selectable && (
                  <td className="w-10">
                    <input
                      type="checkbox"
                      className="ops-checkbox"
                      aria-label={
                        canSelect
                          ? `Select job ${j.job_id}`
                          : `Cannot delete live or in-flight job ${j.job_id}`
                      }
                      checked={isChecked}
                      disabled={!canSelect}
                      onChange={() => {
                        if (canSelect) onToggleSelect?.(j.job_id)
                      }}
                      title={
                        canSelect
                          ? 'Select for delete'
                          : 'Cannot delete a live or in-flight job'
                      }
                    />
                  </td>
                )}
                <td>
                  <button
                    type="button"
                    className="font-mono text-[11px] text-text-secondary hover:text-accent-text hover:underline"
                    title={`${j.job_id} — open job`}
                    onClick={() => onOpenJob(j.issue_key, j.job_id)}
                  >
                    {j.job_id.length > 22 ? `${j.job_id.slice(0, 20)}…` : j.job_id}
                  </button>
                  {j.live && (
                    <span className="ml-1.5 inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-warning-text">
                      <span className="h-1.5 w-1.5 rounded-full bg-warning" aria-hidden />
                      live
                    </span>
                  )}
                  {j.merge_request_url && (
                    <div className="mt-0.5">
                      <a
                        href={j.merge_request_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[10px] text-accent-text hover:underline"
                        title={j.merge_request_url}
                        onClick={(e) => e.stopPropagation()}
                      >
                        MR
                      </a>
                      {j.commit_sha && (
                        <span className="ml-1.5 font-mono text-[10px] text-text-muted">
                          {j.commit_sha.slice(0, 7)}
                        </span>
                      )}
                    </div>
                  )}
                  {!j.merge_request_url &&
                    j.delivery_status === 'no_new_commits' && (
                      <div
                        className="mt-0.5 text-[10px] text-text-muted"
                        title={j.delivery_note || 'No new commits this run'}
                      >
                        no new commits
                      </div>
                    )}
                </td>
                <td>
                  <button
                    type="button"
                    className="font-mono text-sm text-accent-text hover:underline"
                    title="Open issue / task page"
                    onClick={() =>
                      onOpenIssue
                        ? onOpenIssue(j.issue_key)
                        : onOpenJob(j.issue_key, j.job_id)
                    }
                  >
                    {j.issue_key}
                  </button>
                  <div
                    className="mt-0.5 max-w-xs truncate text-xs text-text-secondary"
                    title={j.summary}
                  >
                    {j.summary || '—'}
                  </div>
                </td>
                <td>
                  <StatusBadge status={j.status} size={dense ? 'sm' : 'md'} />
                  {j.error_message && (
                    <div className="mt-1 max-w-xs truncate text-xs text-danger-text">
                      {j.error_message}
                    </div>
                  )}
                </td>
                {!compact && (
                  <td className="text-text-secondary">
                    {j.workflow_type}
                    {j.agent ? (
                      <div className="text-[10px] text-text-muted">{j.agent}</div>
                    ) : null}
                  </td>
                )}
                <td className="font-mono text-[11px] text-text-muted">
                  {j.started_at ?? '—'}
                </td>
                {!compact && showProgress && (
                  <td>
                    {showBar || !terminal ? (
                      <div className="flex items-center gap-2">
                        <div className="h-1 w-14 overflow-hidden rounded-full bg-border">
                          <div
                            className="h-full rounded-full bg-accent"
                            style={{
                              width: `${Math.min(100, j.progress_percentage)}%`,
                            }}
                          />
                        </div>
                        <span className="font-mono text-xs text-text-muted">
                          {j.progress_percentage}%
                        </span>
                      </div>
                    ) : (
                      <span className="font-mono text-xs text-text-muted">
                        {j.progress_percentage}%
                      </span>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
