/**
 * Run: npx tsx src/pages/pageSectionUrl.test.ts
 */
import {
  analyticsBackHref,
  analyticsPeriodFromParam,
  analyticsPeriodPath,
} from './analytics/analyticsPeriodUrl'
import { issueTabFromSection, issueTabPath } from './issues/issueTabUrl'
import { jobsFilterFromPath, jobsFilterPath, jobsPageFromPath } from './jobs/jobsFilterUrl'
import { listPageFromSegment, withListPage } from '../util/listPageUrl'
import {
  canonicalSchedulePath,
  parseSchedulePath,
  scheduleHere,
  schedulePath,
} from './schedules/scheduleTabUrl'
import {
  canonicalSettingsPath,
  settingsHere,
  settingsSectionFromParam,
  settingsSectionPath,
} from './settings/settingsSectionUrl'

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

assert(issueTabFromSection(undefined) === 'overview', 'issue default is overview')
assert(issueTabFromSection('logs') === 'logs', 'issue logs section')
assert(issueTabFromSection('nope') === null, 'unknown issue section')
assert(issueTabPath('KAN-1', 'overview') === '/tasks/KAN-1', 'issue overview has no suffix')
assert(issueTabPath('KAN-1', 'logs') === '/tasks/KAN-1/logs', 'issue logs suffix')

assert(settingsSectionFromParam(undefined) === 'jira', 'settings default is jira')
assert(settingsSectionFromParam('agent') === 'model', 'Agent url opens the model section')
assert(settingsSectionFromParam('nope') === null, 'unknown settings section')
assert(settingsSectionPath('jira') === '/settings/jira', 'jira section names jira')
assert(settingsSectionPath('model') === '/settings/agent', 'model section is /agent')
assert(settingsSectionPath('gitlab') === '/settings/gitlab', 'gitlab suffix')
assert(settingsSectionPath('azure') === '/settings/azure', 'azure settings suffix')
assert(settingsSectionPath('projects') === '/settings/projects', 'projects suffix')
assert(settingsSectionPath('runtime') === '/settings/runtime', 'runtime suffix')
assert(settingsHere(undefined) === '/settings', 'bare settings address')
assert(settingsHere('Jira') === '/settings/Jira', 'settings here keeps the typed segment')
assert(canonicalSettingsPath(undefined) === '/settings/jira', 'bare settings becomes /jira')
assert(canonicalSettingsPath('') === '/settings/jira', 'empty settings becomes /jira')
assert(canonicalSettingsPath('nope') === '/settings/jira', 'unknown settings becomes /jira')
assert(canonicalSettingsPath('agent') === '/settings/agent', 'agent stays on /agent')
assert(canonicalSettingsPath('Jira') === '/settings/jira', 'Jira segment is normalized')

assert(jobsFilterFromPath('/jobs') === 'all', 'jobs default is all')
assert(jobsFilterFromPath('/jobs/in-flight') === 'active', 'in-flight filter')
assert(jobsFilterFromPath('/jobs/queue') === 'queue', 'queue filter')
assert(jobsFilterPath('all') === '/jobs', 'all jobs has no suffix')
assert(jobsFilterPath('active') === '/jobs/in-flight', 'in-flight path')
assert(jobsFilterPath('cancelled') === '/jobs/cancelled', 'cancelled path')
assert(jobsFilterPath('error') === '/jobs/error', 'error path')
assert(jobsFilterPath('completed') === '/jobs/completed', 'completed path')
assert(jobsFilterPath('plan_ready') === '/jobs/plan-ready', 'plan ready path')
assert(jobsFilterFromPath('/jobs/plan-ready') === 'plan_ready', 'plan ready filter')
assert(jobsPageFromPath('/jobs/plan-ready/2') === 2, 'plan ready page is the last segment')
assert(jobsFilterPath('queue') === '/jobs/queue', 'queue path')
assert(jobsFilterFromPath('/jobs/job_1') === 'all', 'a job id is not a list filter')
assert(jobsFilterFromPath('/jobs/queue/2') === 'queue', 'queue page keeps the queue filter')
assert(jobsPageFromPath('/jobs') === 1, 'jobs page 1 has no suffix')
assert(jobsPageFromPath('/jobs/2') === 2, 'jobs page is the last segment')
assert(jobsPageFromPath('/jobs/queue/3') === 3, 'filtered jobs page is the last segment')
assert(jobsPageFromPath('/jobs/job_1') === 1, 'a job id is not a page number')
assert(listPageFromSegment('0') === null, 'page 0 is not a page segment')
assert(withListPage('/jobs/queue', 1) === '/jobs/queue', 'page 1 omits the number')
assert(withListPage('/sessions', 4) === '/sessions/4', 'later pages append the number')

assert(analyticsPeriodFromParam(undefined) === '30d', 'analytics default is 30d')
assert(analyticsPeriodFromParam('7d') === '7d', '7d period')
assert(analyticsPeriodFromParam('reviews') === null, 'reviews is not a period')
assert(analyticsPeriodPath('30d') === '/analytics', 'default period has no suffix')
assert(analyticsPeriodPath('custom') === '/analytics/custom', 'custom period suffix')
assert(analyticsPeriodPath('24h') === '/analytics/24h', '24h suffix')
assert(analyticsPeriodPath('90d') === '/analytics/90d', '90d suffix')
assert(analyticsPeriodPath('1y') === '/analytics/1y', '1y suffix')
assert(analyticsPeriodPath('all') === '/analytics/all', 'all suffix')
assert(analyticsPeriodFromParam('24H') === '24h', 'period match is case-insensitive')
assert(
  analyticsBackHref('period=7d&state=opened&origin=ours') === '/analytics/7d',
  'back from a 7 day review list opens /analytics/7d',
)
assert(
  analyticsBackHref('state=merged') === '/analytics',
  'a review list with no period returns to the 30 day chart',
)
assert(
  analyticsBackHref('period=all&from=2026-09-01T00:00&to=2026-09-07T00:00&origin=ours') ===
    '/analytics/custom?from=2026-09-01T00%3A00&to=2026-09-07T00%3A00',
  'a custom range returns to /analytics/custom with the same from and to',
)

assert(parseSchedulePath(undefined, undefined)?.mode === 'existing', 'schedule default mode')
assert(parseSchedulePath('azure', undefined)?.tracker === 'azure', 'existing azure path')
assert(parseSchedulePath('new', 'azure')?.mode === 'new', 'new azure path')
assert(parseSchedulePath('mr', 'azure') === null, 'mr has no azure tracker')
assert(schedulePath('existing', 'jira') === '/scheduled/jira', 'existing jira names jira')
assert(schedulePath('existing', 'azure') === '/scheduled/azure', 'existing azure suffix')
assert(schedulePath('new', 'jira') === '/scheduled/new/jira', 'new jira names jira')
assert(schedulePath('mr') === '/scheduled/mr', 'mr suffix')
assert(schedulePath('new', 'azure') === '/scheduled/new/azure', 'new azure suffix')
assert(schedulePath('pr') === '/scheduled/pr', 'pr suffix')
assert(parseSchedulePath('new', undefined)?.tracker === 'jira', 'new without a tracker is jira')
assert(parseSchedulePath('mr', undefined)?.mode === 'mr', 'mr has no tracker')
assert(parseSchedulePath('nope', undefined) === null, 'unknown schedule mode')
assert(scheduleHere(undefined, undefined) === '/scheduled', 'bare scheduled address')
assert(scheduleHere('new', undefined) === '/scheduled/new', 'new without jira is the typed address')
assert(scheduleHere('new', 'jira') === '/scheduled/new/jira', 'typed new jira address')
assert(canonicalSchedulePath(undefined, undefined) === '/scheduled/jira', 'bare scheduled becomes /jira')
assert(canonicalSchedulePath('new', undefined) === '/scheduled/new/jira', 'new fills in jira')
assert(canonicalSchedulePath('new', '') === '/scheduled/new/jira', 'empty tracker fills in jira')
assert(canonicalSchedulePath('new', 'jira') === '/scheduled/new/jira', 'explicit new jira stays')
assert(canonicalSchedulePath('new', 'azure') === '/scheduled/new/azure', 'new azure stays')
assert(canonicalSchedulePath('azure', undefined) === '/scheduled/azure', 'existing azure stays')
assert(canonicalSchedulePath('mr', undefined) === '/scheduled/mr', 'mr is not given a jira suffix')
assert(canonicalSchedulePath('pr', 'jira') === '/scheduled/pr', 'pr ignores a jira tracker')
assert(canonicalSchedulePath('nope', undefined) === '/scheduled/jira', 'unknown mode becomes existing jira')
assert(parseSchedulePath('jira', '2')?.page === 2, 'scheduled jira page is the last segment')
assert(parseSchedulePath('new', 'jira', '3')?.page === 3, 'new jira page is the last segment')
assert(parseSchedulePath('mr', '4')?.mode === 'mr', 'mr page does not become a tracker')
assert(canonicalSchedulePath('jira', '2') === '/scheduled/jira/2', 'scheduled page stays on the jira path')
assert(canonicalSchedulePath('new', 'azure', '2') === '/scheduled/new/azure/2', 'new azure page suffix')
assert(canonicalSchedulePath('mr', '2') === '/scheduled/mr/2', 'mr page suffix')
assert(canonicalSchedulePath('2', undefined) === '/scheduled/jira/2', 'bare page becomes existing jira')
assert(scheduleHere('jira', '2') === '/scheduled/jira/2', 'typed scheduled page is kept')
assert(
  scheduleHere('new', undefined) !== canonicalSchedulePath('new', undefined),
  'new without jira must be replaced',
)

if (failures.length) throw new Error(failures.join('\n'))
console.log('pageSectionUrl ok')
