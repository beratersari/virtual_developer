/**
 * Run: npx tsx src/pages/pageSectionUrl.test.ts
 */
import { analyticsPeriodFromParam, analyticsPeriodPath } from './analytics/analyticsPeriodUrl'
import { issueTabFromSection, issueTabPath } from './issues/issueTabUrl'
import { jobsFilterFromPath, jobsFilterPath } from './jobs/jobsFilterUrl'
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
assert(jobsFilterPath('queue') === '/jobs/queue', 'queue path')
assert(jobsFilterFromPath('/jobs/job_1') === 'all', 'a job id is not a list filter')

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
assert(
  scheduleHere('new', undefined) !== canonicalSchedulePath('new', undefined),
  'new without jira must be replaced',
)

if (failures.length) throw new Error(failures.join('\n'))
console.log('pageSectionUrl ok')
