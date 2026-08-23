import type { JobItem, JobRetryAttempt } from '../../api/types'
import { IN_FLIGHT_STATUSES } from '../../util/status'
import { pathBasename } from '../../util/paths'
import { resolveJobWorker, sessionKindLabel, workerLabel } from '../../util/worker'
import { LiveDot } from '../../ui/LiveDot'
import { MetaCard } from '../../ui/MetaCard'
import { StatusBadge } from '../../ui/StatusBadge'

export function JobOverview({
  job,
  elapsedLabel,
  fallbackWorker = '',
}: {
  job: JobItem
  elapsedLabel: string
  fallbackWorker?: string
}) {
  const retries: JobRetryAttempt[] = job.retry_attempts || []
  const worker = resolveJobWorker(job, fallbackWorker)
  const showDelivery =
    job.merge_request_url ||
    job.commit_url ||
    job.commit_sha ||
    (job.feature_branch && job.delivery_status !== 'no_new_commits')

  return (
    <div className="space-y-6 text-sm">
      <div className="rounded border border-border bg-bg px-4 py-3">
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
          {(job.source || 'jira') === 'gitlab' ? 'GitLab MR comment' : 'Jira description'}
        </div>
        {job.description?.trim() ? (
          <p className="whitespace-pre-wrap text-sm text-text-secondary">{job.description}</p>
        ) : (
          <p className="text-sm italic text-text-muted">
            {(job.source || 'jira') === 'gitlab'
              ? 'No MR comment body was stored when this job started.'
              : 'No Jira description was stored when this job started.'}
          </p>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <MetaCard label="Job id" mono value={job.job_id} />
        <MetaCard label="Status" valueNode={<StatusBadge status={job.status} />} />
        <MetaCard label="Progress" value={`${job.progress_percentage}%`} />
        <MetaCard label="Workflow" value={job.workflow_type || '—'} />
        <MetaCard label="Worker" value={workerLabel(worker)} />
        <MetaCard
          label="Model"
          mono
          value={job.model?.trim() ? job.model : '—'}
        />
        <MetaCard label="Issue" mono value={job.issue_key || '—'} />
        <MetaCard
          label="Source"
          value={(job.source || 'jira') === 'gitlab' ? 'GitLab MR' : 'Jira'}
        />
        {job.gitlab_project && (
          <MetaCard
            label="GitLab project"
            mono
            value={
              job.gitlab_mr_iid
                ? `${job.gitlab_project}!${job.gitlab_mr_iid}`
                : job.gitlab_project
            }
          />
        )}
        <MetaCard
          label="Working folder"
          mono
          className="sm:col-span-2 lg:col-span-3"
          value={job.working_directory || '—'}
        />
        <MetaCard label="Started" mono value={job.started_at ?? '—'} />
        <MetaCard label="Completed" mono value={job.completed_at ?? '—'} />
        <MetaCard
          label="Elapsed"
          valueNode={
            elapsedLabel === '—' ? (
              <span className="text-sm text-text">—</span>
            ) : (
              <div className="flex items-baseline gap-2">
                <span className="font-mono text-sm tabular-nums text-text">{elapsedLabel}</span>
                {!job.completed_at &&
                  (job.live || IN_FLIGHT_STATUSES.has((job.status || '').toLowerCase())) && (
                    <LiveDot />
                  )}
              </div>
            )
          }
        />
        {job.error_message && (
          <div className="sm:col-span-2 lg:col-span-3">
            <div className="mb-1 text-xs font-medium text-danger-text">Error</div>
            <pre className="vd-pre max-h-48 text-danger-text">{job.error_message}</pre>
          </div>
        )}
      </div>

      {job.delivery_status === 'no_new_commits' && (
        <div className="vd-alert vd-alert-warning">
          <p className="text-sm font-medium">Completed with no new commits</p>
          <p className="mt-1 text-xs text-text-secondary">
            {job.delivery_note || 'Agent finished successfully; HEAD did not change for this job.'}
          </p>
        </div>
      )}

      {showDelivery && (
        <div className="grid gap-3 border-t border-border pt-4 sm:grid-cols-2 lg:grid-cols-3">
          {job.feature_branch && <MetaCard label="Branch" mono value={job.feature_branch} />}
          {(job.commit_url || job.commit_sha) && (
            <MetaCard
              label="Commit"
              className="sm:col-span-2"
              valueNode={
                job.commit_url ? (
                  <div className="space-y-1">
                    <a
                      href={job.commit_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="break-all text-sm text-accent-text hover:underline"
                    >
                      {job.commit_sha ? job.commit_sha.slice(0, 12) : 'Open commit'}
                    </a>
                    {job.commit_subject && (
                      <div className="break-words text-xs text-text-secondary">{job.commit_subject}</div>
                    )}
                  </div>
                ) : (
                  <span className="font-mono text-xs">{job.commit_sha}</span>
                )
              }
            />
          )}
          {job.merge_request_url && (
            <MetaCard
              label="Merge request"
              className="sm:col-span-2 lg:col-span-3"
              valueNode={
                <a
                  href={job.merge_request_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="break-all text-sm text-accent-text hover:underline"
                >
                  {job.merge_request_url}
                </a>
              }
            />
          )}
        </div>
      )}

      <div className="border-t border-border pt-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <MetaCard label="Task id (latest)" mono value={job.task_id ?? '—'} />
          <MetaCard
            label={sessionKindLabel(worker) === 'thread' ? 'Codex thread' : 'Session'}
            mono
            value={job.opencode_session_id ?? '—'}
          />
          <MetaCard
            label="Run log (latest)"
            mono
            className="sm:col-span-2 lg:col-span-3"
            value={job.session_log_path ?? '—'}
          />
        </div>
        {retries.length > 0 && (
          <div className="mt-4 overflow-x-auto rounded border border-border">
            <table className="w-full min-w-[32rem] text-left text-xs">
              <thead className="bg-bg-elevated text-[10px] uppercase tracking-wide text-text-muted">
                <tr>
                  <th className="px-3 py-2">Label</th>
                  <th className="px-3 py-2">Reason</th>
                  <th className="px-3 py-2">Return</th>
                  <th className="px-3 py-2">Error</th>
                  <th className="px-3 py-2">Failed log</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {retries.map((r, i) => (
                  <tr key={`${r.label}-${r.timestamp || i}`}>
                    <td className="px-3 py-2 font-mono">_{r.label || `retry${r.attempt_number}`}</td>
                    <td className="px-3 py-2">{r.reason || '—'}</td>
                    <td className="px-3 py-2 font-mono">{r.return_code ?? '—'}</td>
                    <td className="max-w-xs truncate px-3 py-2 font-mono">
                      {r.error_message ? r.error_message.slice(0, 160) : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-text-muted">
                      {r.failed_session_log_path ? pathBasename(r.failed_session_log_path) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
