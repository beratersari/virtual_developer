/**
 * Run: npx tsx src/pages/jobs/jobSourceLabel.test.ts
 */
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { JobItem } from '../../api/types'
import { JobOverview } from './JobOverview'
import { JobsTable } from './JobsTable'

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

function job(source: string): JobItem {
  return {
    job_id: 'job_azure_wi',
    issue_key: '42',
    summary: 'Board task',
    workflow_type: 'execution',
    agent: 'build',
    status: 'completed',
    live: false,
    source,
    azure_project: 'Fabrikam',
  }
}

const overview = renderToStaticMarkup(
  createElement(JobOverview, { job: job('azure_workitem'), elapsedLabel: '—' }),
)
const sourceAt = overview.indexOf('Source')
const sourceCard = overview.slice(sourceAt, sourceAt + 180)
assert(
  sourceCard.includes('Azure') && !sourceCard.includes('Jira'),
  `azure_workitem detail must not be labeled Jira: ${sourceCard}`,
)

const pr = renderToStaticMarkup(
  createElement(JobOverview, { job: job('azure'), elapsedLabel: '—' }),
)
assert(pr.includes('Azure PR'), 'azure PR jobs stay Azure PR')

const list = renderToStaticMarkup(
  createElement(JobsTable, {
    jobs: [job('azure_workitem')],
    onOpenJob: () => undefined,
  }),
)
assert(
  list.includes('Azure'),
  'azure_workitem list row must show an Azure chip, not look like Jira',
)

if (failures.length) {
  throw new Error(failures.join('\n'))
}

console.log('jobSourceLabel.test.ts ok')
