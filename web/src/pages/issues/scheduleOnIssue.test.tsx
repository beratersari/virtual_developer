/**
 * Run: npx tsx --tsconfig tsconfig.app.json src/pages/issues/scheduleOnIssue.test.tsx
 */
import { renderToStaticMarkup } from 'react-dom/server'
import {
  ScheduleOnIssue,
  issueDescriptionLabel,
  jobsEmptyLabel,
  schedulePrompt,
  scheduleRepositories,
} from './scheduledTicket'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const prompt = 'Add a health check\n\n{code}\n{params}\nMode: build\n{params}\n{code}'
const row = {
  schedule_id: 'sched_1',
  title: 'Health endpoint',
  description: 'short operator text',
  issue_description: prompt,
  repository_url: 'https://gitlab.example.com/acme/api.git',
  source_branch: 'develop',
  target_branch: 'main',
  repository_refs: [
    {
      url: 'https://gitlab.example.com/acme/api.git',
      source_branch: 'develop',
      target_branch: 'main',
    },
  ],
  mode: 'build',
  model: 'opencode/hy3-free',
  backend: 'opencode',
  scheduled_at: '2026-10-09T18:30:00',
  status: 'scheduled',
  source: 'new',
}

assert(schedulePrompt(row) === prompt, 'full snapshot is the prompt')
assert(schedulePrompt({ description: 'only short' }) === 'only short', 'short copy is the fallback')
assert(scheduleRepositories({ schedule_id: 's', repository_url: 'https://gitlab.example/a.git', source_branch: 'develop', target_branch: 'main' })[0].source === 'develop', 'single repo uses the primary branches')
assert(issueDescriptionLabel({ schedules: [row], description: prompt }) === 'Scheduled prompt', 'matching text is the scheduled prompt')
assert(issueDescriptionLabel({ jiraLive: true, schedules: [row], description: prompt }) === 'Live issue description', 'live jira keeps its label')
assert(jobsEmptyLabel(1) === 'No run yet.', 'a schedule with no job is not an empty filter')
assert(jobsEmptyLabel(0) === 'Nothing here for this filter.', 'no schedule keeps the filter copy')

const html = renderToStaticMarkup(
  <ScheduleOnIssue schedules={[row]} shownDescription="" jiraHost="" />,
)
assert(html.includes('Add a health check'), 'prompt is on the ticket')
assert(html.includes('https://gitlab.example.com/acme/api.git'), 'repository is on the ticket')
assert(html.includes('develop'), 'source branch is on the ticket')
assert(html.includes('main'), 'target branch is on the ticket')
assert(html.includes('build'), 'mode is on the ticket')
assert(html.includes('opencode/hy3-free'), 'model is on the ticket')
assert(html.includes('2026-10-09 18:30'), 'scheduled time is on the ticket')
assert(html.includes('Scheduled'), 'status label is on the ticket')
assert(html.includes('Health endpoint'), 'title is on the ticket')

const same = renderToStaticMarkup(
  <ScheduleOnIssue schedules={[row]} shownDescription={prompt} />,
)
assert(!same.includes('Add a health check'), 'prompt already in the description is not repeated')
assert(same.includes('https://gitlab.example.com/acme/api.git'), 'details stay when the prompt is above')

console.log('scheduleOnIssue ok')
