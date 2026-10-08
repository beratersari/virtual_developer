import type { TaskSchedule } from '../../api/types'
import { JiraLinkedText } from '../../ui/JiraLinkedText'
import { StatusBadge } from '../../ui/StatusBadge'
import { formatScheduleWhen } from '../../util/time'

export type ScheduleRepo = {
  url: string
  source: string
  target: string
}

/** Text the schedule will run. The full snapshot wins over the short copy. */
export function schedulePrompt(row: {
  description?: string | null
  issue_description?: string | null
}): string {
  return (row.issue_description || row.description || '').trim()
}

export function scheduleKind(source?: string, mode?: string): string {
  const review = (mode || '').trim().toLowerCase() === 'review'
  if (source === 'gitlab_mr') return review ? 'MR review' : 'MR follow-up'
  if (source === 'azure_pr') return review ? 'PR review' : 'PR follow-up'
  if (source === 'new') return 'New issue'
  if (source === 'existing') return 'Existing issue'
  return ''
}

export function scheduleRepositories(row: TaskSchedule): ScheduleRepo[] {
  const refs = (row.repository_refs || [])
    .map((ref) => ({
      url: (ref.url || '').trim(),
      source: (ref.source_branch || '').trim(),
      target: (ref.target_branch || '').trim(),
    }))
    .filter((ref) => ref.url)
  if (refs.length > 0) return refs
  const url = (row.repository_url || '').trim()
  if (!url) return []
  return [
    {
      url,
      source: (row.source_branch || '').trim(),
      target: (row.target_branch || '').trim(),
    },
  ]
}

export function issueDescriptionLabel(input: {
  jiraLive?: boolean
  description?: string | null
  schedules?: { description?: string | null; issue_description?: string | null }[]
}): string {
  if (input.jiraLive) return 'Live issue description'
  const prompt = input.schedules?.length ? schedulePrompt(input.schedules[0]) : ''
  if (prompt && prompt === (input.description || '').trim()) return 'Scheduled prompt'
  return 'Issue description'
}

/** Jobs table copy when the ticket has a schedule and no run yet. */
export function jobsEmptyLabel(scheduleCount: number): string {
  return scheduleCount > 0 ? 'No run yet.' : 'Nothing here for this filter.'
}

function requestLabel(row: TaskSchedule): string {
  if (row.source === 'gitlab_mr' && row.gitlab_project && row.mr_iid) {
    return `${row.gitlab_project}!${row.mr_iid}`
  }
  if (row.source === 'azure_pr' && row.azure_project && row.pr_id) {
    return `${row.azure_project}!${row.pr_id}`
  }
  return row.merge_request_url || ''
}

export function ScheduleOnIssue({
  schedules,
  shownDescription = '',
  jiraHost = '',
}: {
  schedules: TaskSchedule[]
  shownDescription?: string
  jiraHost?: string
}) {
  if (schedules.length === 0) return null
  const shown = shownDescription.trim()
  return (
    <div className="space-y-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
        Scheduled
      </div>
      {schedules.map((row) => {
        const prompt = schedulePrompt(row)
        const showPrompt = Boolean(prompt) && prompt !== shown
        const repos = scheduleRepositories(row)
        const kind = scheduleKind(row.source, row.mode)
        const request = requestLabel(row)
        return (
          <article key={row.schedule_id} className="vd-card space-y-3 px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <StatusBadge status={row.status || 'scheduled'} size="sm" />
              {kind ? <span className="text-xs text-text-muted">{kind}</span> : null}
              <span className="text-xs text-text-muted">{formatScheduleWhen(row.scheduled_at)}</span>
              {row.mode ? <span className="font-mono text-xs text-text">{row.mode}</span> : null}
              {row.model ? (
                <span className="font-mono text-xs text-text-muted">{row.model}</span>
              ) : null}
              {row.backend ? (
                <span className="font-mono text-xs text-text-muted">{row.backend}</span>
              ) : null}
            </div>
            {row.title ? <p className="text-sm text-text">{row.title}</p> : null}
            {repos.length > 0 && (
              <ul className="space-y-1">
                {repos.map((repo, index) => (
                  <li
                    key={`${repo.url}|${repo.source}|${repo.target}|${index}`}
                    className="break-all font-mono text-xs text-text-secondary"
                  >
                    {repo.url}
                    {(repo.source || repo.target) && (
                      <span className="text-text-muted">
                        {' '}
                        · {repo.source || '—'} → {repo.target || '—'}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {row.merge_request_url && request ? (
              <a
                href={row.merge_request_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-block break-all text-sm text-accent-text hover:underline"
              >
                {request}
              </a>
            ) : null}
            {row.error_message ? (
              <pre className="vd-pre max-h-48 text-danger-text">{row.error_message}</pre>
            ) : null}
            {showPrompt ? (
              <div>
                <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
                  Prompt
                </div>
                <pre className="max-h-[min(60vh,36rem)] overflow-auto whitespace-pre-wrap rounded border border-border bg-bg p-4 font-mono text-xs leading-relaxed text-text">
                  <JiraLinkedText text={prompt} jiraHost={jiraHost} />
                </pre>
              </div>
            ) : null}
          </article>
        )
      })}
    </div>
  )
}
