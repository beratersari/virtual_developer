import type { JobItem, JobRetryAttempt } from '../../api/types'
import { pathBasename } from '../../util/paths'
import { jobChannelLabel } from '../../util/jobChannel'
import { resolveJobWorker, sessionIdLabel, workerLabel } from '../../util/worker'
import { MetaCard } from '../../ui/MetaCard'
import { JiraLinkedText } from '../../ui/JiraLinkedText'
import { groupDeliveries } from './repoDeliveries'
import type { GitDelivery } from '../../api/types'

export function JobOverview({
  job,
  fallbackWorker = '',
  jiraHost = '',
}: {
  job: JobItem
  fallbackWorker?: string
  jiraHost?: string
}) {
  const retries: JobRetryAttempt[] = job.retry_attempts || []
  const worker = resolveJobWorker(job, fallbackWorker)
  const deliveryRows: GitDelivery[] =
    job.deliveries && job.deliveries.length > 0
      ? job.deliveries
      : job.merge_request_url || job.commit_url || job.commit_sha || job.feature_branch
        ? [
            {
              feature_branch: job.feature_branch,
              merge_request_url: job.merge_request_url,
              commit_sha: job.commit_sha,
              commit_subject: job.commit_subject,
              commit_url: job.commit_url,
            },
          ]
        : []
  const deliveryGroups = groupDeliveries(deliveryRows)

  return (
    <div className="space-y-6 text-sm">
      <div className="rounded border border-border bg-bg px-4 py-3">
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
          Description
        </div>
        {job.description?.trim() ? (
          <p className="whitespace-pre-wrap text-sm text-text-secondary">
            <JiraLinkedText text={job.description} jiraHost={jiraHost} />
          </p>
        ) : (
          <p className="text-sm italic text-text-muted">
            No description was stored when this job started.
          </p>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <MetaCard label="Job id" mono value={job.job_id} />
        <MetaCard
          label="Workflow"
          value={jobChannelLabel(job) || job.workflow_type || '—'}
        />
        <MetaCard label="Worker" value={workerLabel(worker)} />
        <MetaCard
          label="Model"
          mono
          value={job.model?.trim() ? job.model : '—'}
        />
        <MetaCard
          label="Source"
          value={
            (job.source || 'jira') === 'gitlab'
              ? 'GitLab MR'
              : (job.source || 'jira') === 'azure'
                ? 'Azure PR'
                : (job.source || 'jira') === 'azure_workitem'
                  ? 'Azure Boards'
                  : 'Jira'
          }
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
        {job.azure_project && (
          <MetaCard
            label="Azure project"
            mono
            value={
              job.azure_pr_id
                ? `${job.azure_project}!${job.azure_pr_id}`
                : job.azure_project
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

      {deliveryGroups.length > 0 && (
        <div className="space-y-4 border-t border-border pt-4">
          {deliveryGroups.map((group) => (
            <div key={group.repo} className="space-y-3">
              <div className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                {group.repo}
              </div>
              {group.rows.map((row, index) => (
                <div
                  key={`${group.repo}-${row.commit_sha || row.merge_request_url || index}`}
                  className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
                >
                  {row.feature_branch && (
                    <MetaCard label="Branch" mono value={row.feature_branch} />
                  )}
                  {(row.commit_url || row.commit_sha) && (
                    <MetaCard
                      label="Commit"
                      className="sm:col-span-2"
                      valueNode={
                        row.commit_url ? (
                          <div className="space-y-1">
                            <a
                              href={row.commit_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="break-all text-sm text-accent-text hover:underline"
                            >
                              {row.commit_sha ? row.commit_sha.slice(0, 12) : 'Open commit'}
                            </a>
                            {row.commit_subject && (
                              <div className="break-words text-xs text-text-secondary">
                                {row.commit_subject}
                              </div>
                            )}
                          </div>
                        ) : (
                          <span className="font-mono text-xs">{row.commit_sha}</span>
                        )
                      }
                    />
                  )}
                  {row.merge_request_url && (
                    <MetaCard
                      label="Merge request"
                      className="sm:col-span-2 lg:col-span-3"
                      valueNode={
                        <a
                          href={row.merge_request_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="break-all text-sm text-accent-text hover:underline"
                        >
                          {row.merge_request_url}
                        </a>
                      }
                    />
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <div className="border-t border-border pt-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <MetaCard label="Task id (latest)" mono value={job.task_id ?? '—'} />
          <MetaCard
            label={sessionIdLabel(worker)}
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
