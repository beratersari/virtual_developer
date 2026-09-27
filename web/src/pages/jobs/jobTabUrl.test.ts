/**
 * Run: npx tsx src/pages/jobs/jobTabUrl.test.ts
 */
import { jobTabFromSection, jobTabPath } from './jobTabUrl'

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

assert(jobTabFromSection(undefined) === 'overview', 'bare job url is Details')
assert(jobTabFromSection('') === 'overview', 'empty section is Details')
assert(jobTabFromSection('transcript') === 'chat', 'transcript opens the Transcript tab')
assert(jobTabFromSection('Transcript') === 'chat', 'section match is case-insensitive')
assert(jobTabFromSection('plan') === 'plan', 'plan section')
assert(jobTabFromSection('prompt') === 'prompt', 'prompt section')
assert(jobTabFromSection('output') === 'output', 'output section')
assert(jobTabFromSection('daemon') === 'logs', 'daemon opens the Daemon tab')
assert(jobTabFromSection('nope') === null, 'unknown section is not a tab')

assert(jobTabPath('job_1', 'overview') === '/jobs/job_1', 'Details has no suffix')
assert(jobTabPath('job_1', 'chat') === '/jobs/job_1/transcript', 'Transcript suffix')
assert(jobTabPath('job a/b', 'chat') === '/jobs/job%20a%2Fb/transcript', 'job id is encoded')
assert(jobTabPath('job_1', 'logs') === '/jobs/job_1/daemon', 'Daemon suffix')
assert(jobTabPath('job_1', 'plan') === '/jobs/job_1/plan', 'Plan suffix')

if (failures.length) {
  throw new Error(failures.join('\n'))
}
console.log('jobTabUrl ok')
