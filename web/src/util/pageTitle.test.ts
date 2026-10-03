/**
 * Run: npx tsx src/util/pageTitle.test.ts
 */
import {
  formatPageTitle,
  isRecordPlaceholder,
  issuePageName,
  jobPageName,
  pageNameFromLocation,
  resolveDocumentTitle,
  recordTitle,
  shownPageName,
  visiblePageNameFrom,
  reviewsPageName,
  workspacePageName,
} from './pageTitle'

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

function titled(path: string, search = ''): string {
  return formatPageTitle(pageNameFromLocation(path, search))
}

assert(formatPageTitle('') === 'Yaver', 'empty name stays Yaver')
assert(formatPageTitle('Yaver') === 'Yaver', 'brand alone is not doubled')
assert(formatPageTitle('Yaver - Jobs') === 'Yaver - Jobs', 'prefix is not doubled')
assert(formatPageTitle('  Jobs  ') === 'Yaver - Jobs', 'name is trimmed')

assert(titled('/jobs') === 'Yaver - Jobs', 'jobs')
assert(titled('/jobs/2') === 'Yaver - Jobs', 'jobs page 2 keeps the list name')
assert(titled('/jobs/in-flight') === 'Yaver - Jobs - In flight', 'in flight')
assert(titled('/jobs/in-flight/3') === 'Yaver - Jobs - In flight', 'in flight page')
assert(titled('/jobs/queue') === 'Yaver - Jobs - Queue', 'queue')
assert(titled('/jobs/error') === 'Yaver - Jobs - Error', 'error')
assert(titled('/jobs/completed') === 'Yaver - Jobs - Completed', 'completed')
assert(titled('/jobs/cancelled') === 'Yaver - Jobs - Cancelled', 'cancelled')
assert(titled('/jobs/plan-ready') === 'Yaver - Jobs - Plan ready', 'plan ready')
assert(titled('/queue') === 'Yaver - Jobs', 'old queue path')

assert(jobPageName('Fix login', '', 'KAN-1') === 'Fix login', 'job title')
assert(jobPageName('  Fix\nlogin  ', 'transcript', 'KAN-1') === 'Fix login - Transcript', 'job transcript collapses whitespace')
assert(jobPageName('', 'plan', 'KAN-4') === 'KAN-4 - Plan', 'missing summary uses the issue key')
assert(jobPageName('', '', '') === 'Job', 'job fallback')
assert(jobPageName('Fix login', 'daemon') === 'Fix login - Daemon', 'daemon tab')
assert(jobPageName('Fix login', 'prompt') === 'Fix login - Prompt', 'prompt tab')
assert(jobPageName('Fix login', 'output') === 'Fix login - Output', 'output tab')
assert(titled('/jobs/job_1') === 'Yaver - Job', 'job url before the record loads')
assert(
  shownPageName('Job', { path: '/jobs/job_1', name: 'Fix login' }, '/jobs/job_1') === 'Fix login',
  'open job name replaces the address placeholder',
)
assert(
  shownPageName('Job', { path: '/jobs/job_1', name: 'Fix login' }, '/jobs/job_2') === 'Job',
  'a name from another job does not leak',
)
assert(
  visiblePageNameFrom('Job', 'Yaver - Fix login', false) === 'Fix login',
  'the bar keeps the issue name the server already wrote',
)
assert(
  visiblePageNameFrom('Job - Transcript', 'Yaver - Fix login - Transcript', false) ===
    'Fix login - Transcript',
  'the bar keeps the issue name on a job tab',
)
assert(
  visiblePageNameFrom('Fix login - Transcript', 'Yaver - Job', true) === 'Fix login - Transcript',
  'a loaded issue name replaces the placeholder',
)
assert(
  visiblePageNameFrom('Job', 'Yaver - Jobs', true) === 'Job',
  'opening another job does not keep the previous page name',
)
assert(recordTitle('Fix login', 'Job') === 'Fix login', 'the bar shows the loaded issue name')
assert(
  recordTitle('Fix login - Transcript', 'Job - Transcript') === 'Fix login - Transcript',
  'a job tab keeps the issue name',
)
assert(recordTitle('Job', 'Job') === null, 'a placeholder is not a second title')
assert(recordTitle('Jobs', 'Jobs') === null, 'a list page does not repeat its name')
assert(
  recordTitle('Existing issue', 'Existing issue') === null,
  'a schedule mode is not a record title',
)
assert(titled('/jobs/job_1/transcript') === 'Yaver - Job - Transcript', 'job tab before the record loads')

assert(issuePageName('Board bug', 'KAN-9', '') === 'Board bug', 'issue title')
assert(issuePageName('', 'KAN-9', 'logs') === 'KAN-9 - System logs', 'issue logs before summary')
assert(issuePageName('Board bug', 'KAN-9', 'logs') === 'Board bug - System logs', 'issue logs')
assert(titled('/tasks/KAN-9') === 'Yaver - Issue', 'issue url stays generic until the summary loads')
assert(titled('/tasks/KAN-9/logs') === 'Yaver - Issue - System logs', 'issue logs url stays generic')

assert(titled('/analytics') === 'Yaver - Analytics', 'analytics default')
assert(titled('/analytics/30d') === 'Yaver - Analytics', '30 days is the default analytics page')
assert(titled('/analytics/24h') === 'Yaver - Analytics - 24 hours', '24 hours')
assert(titled('/analytics/7d') === 'Yaver - Analytics - 7 days', '7 days')
assert(titled('/analytics/90d') === 'Yaver - Analytics - 90 days', '90 days')
assert(titled('/analytics/1y') === 'Yaver - Analytics - 1 year', '1 year')
assert(titled('/analytics/all') === 'Yaver - Analytics - All', 'all time')
assert(titled('/analytics/custom') === 'Yaver - Analytics - Custom', 'custom range')
assert(reviewsPageName('ours', 'opened') === 'Opened by us · open', 'review heading')
assert(
  titled('/analytics/reviews', '?origin=ours&state=opened') === 'Yaver - Opened by us · open',
  'review filters',
)
assert(
  titled('/analytics/reviews/2', '?origin=contributed&state=merged') === 'Yaver - Contributed · merged',
  'review page keeps the filter name',
)
assert(titled('/analytics/reviews') === 'Yaver - Merge requests', 'reviews default')

assert(titled('/scheduled') === 'Yaver - Existing issue', 'scheduled default')
assert(titled('/scheduled/jira') === 'Yaver - Existing issue', 'existing jira')
assert(titled('/scheduled/azure') === 'Yaver - Existing issue - Azure work item', 'existing azure')
assert(titled('/scheduled/new') === 'Yaver - New issue', 'new issue')
assert(titled('/scheduled/new/jira') === 'Yaver - New issue', 'new jira issue')
assert(titled('/scheduled/new/azure') === 'Yaver - New issue - Azure work item', 'new azure work item')
assert(titled('/scheduled/new/jira/2') === 'Yaver - New issue', 'new issue list page')
assert(titled('/scheduled/mr') === 'Yaver - Existing MR', 'existing mr')
assert(titled('/scheduled/pr') === 'Yaver - Existing PR', 'existing pr')
assert(titled('/schedules') === 'Yaver - Existing issue', 'old schedules path')

assert(titled('/sessions') === 'Yaver - Sessions', 'sessions')
assert(titled('/sessions/2') === 'Yaver - Sessions', 'sessions page 2')
assert(titled('/sessions/osw_1') === 'Yaver - Workspace', 'workspace before it loads')
assert(workspacePageName('feature/KAN-1', 'develop') === 'feature/KAN-1 → develop', 'workspace branches')
assert(workspacePageName('develop', '') === 'develop', 'workspace without a target')

assert(titled('/storage') === 'Yaver - Storage', 'storage')
assert(titled('/poll') === 'Yaver - Board', 'board')
assert(titled('/settings') === 'Yaver - Settings - Jira', 'settings default')
assert(titled('/settings/jira') === 'Yaver - Settings - Jira', 'jira settings')
assert(titled('/settings/gitlab') === 'Yaver - Settings - GitLab', 'gitlab settings')
assert(titled('/settings/azure') === 'Yaver - Settings - Azure', 'azure settings')
assert(titled('/settings/projects') === 'Yaver - Settings - Projects', 'projects settings')
assert(titled('/settings/agent') === 'Yaver - Settings - Agent', 'agent settings')
assert(titled('/settings/runtime') === 'Yaver - Settings - Runtime', 'runtime settings')
assert(titled('/missing') === 'Yaver - Jobs', 'unknown path lands on jobs')

assert(isRecordPlaceholder('Job') && isRecordPlaceholder('Job - Transcript'), 'job placeholder')
assert(isRecordPlaceholder('Issue - System logs'), 'issue placeholder')
assert(!isRecordPlaceholder('Jobs') && !isRecordPlaceholder('Fix login'), 'real names are not placeholders')
const kept = resolveDocumentTitle('Yaver - Fix login - Transcript', 'Job - Transcript', false)
assert(kept.title === 'Yaver - Fix login - Transcript' && kept.seenRealTitle === false, 'fresh load keeps the server job title')
const ready = resolveDocumentTitle(kept.title, 'Fix login - Transcript', kept.seenRealTitle)
assert(ready.title === 'Yaver - Fix login - Transcript' && ready.seenRealTitle, 'loaded job title is applied')
const moved = resolveDocumentTitle('Yaver - Fix login', 'Job', true)
assert(moved.title === 'Yaver - Job', 'a later navigation can show the placeholder')
const issueKept = resolveDocumentTitle('Yaver - Board bug - System logs', 'Issue - System logs', false)
assert(
  issueKept.title === 'Yaver - Board bug - System logs' && issueKept.seenRealTitle === false,
  'fresh load keeps the server issue title',
)
const issueReady = resolveDocumentTitle(issueKept.title, 'Board bug - System logs', issueKept.seenRealTitle)
assert(issueReady.title === 'Yaver - Board bug - System logs' && issueReady.seenRealTitle, 'loaded issue title is applied')

if (failures.length) {
  throw new Error(failures.join('\n'))
}
console.log('pageTitle ok')
