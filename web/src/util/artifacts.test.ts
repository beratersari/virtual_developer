/**
 * Run: npx tsx src/util/artifacts.test.ts
 */
import {
  acceptJobArtifactsResponse,
  artifactsHaveContent,
  jobArtifactPathSignature,
  shouldRefetchJobArtifacts,
} from './artifacts'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(jobArtifactPathSignature({}) === '', 'empty job sig')
assert(
  jobArtifactPathSignature({
    session_log_path: '/s/a.log',
    session_log_paths: ['/s/a.log'],
    prompt_path: '/s/a.prompt.txt',
  }) === '/s/a.log|/s/a.log|/s/a.prompt.txt',
  'sig includes prompt + session',
)

assert(
  artifactsHaveContent([], [{ content: '' }]) === false,
  'blank log is not content',
)
assert(
  artifactsHaveContent([], [{ content: '  hello  ' }]) === true,
  'log text counts',
)
assert(
  artifactsHaveContent([{ error: 'missing' }], []) === true,
  'read error counts so we do not spin',
)

const base = {
  jobId: 'job_1',
  lastJobId: 'job_1',
  pathSignature: '/s/a.log',
  lastPathSignature: '/s/a.log',
  lastHadContent: true,
}

assert(shouldRefetchJobArtifacts({ ...base, jobId: '' }) === false, 'no id')
assert(
  shouldRefetchJobArtifacts({ ...base, lastJobId: '' }) === true,
  'first fetch for this job',
)
assert(shouldRefetchJobArtifacts(base) === false, 'stable completed job')
assert(
  shouldRefetchJobArtifacts({ ...base, force: true }) === true,
  'explicit refresh',
)
assert(
  shouldRefetchJobArtifacts({ ...base, live: true }) === true,
  'live run keeps reading the growing log',
)
assert(
  shouldRefetchJobArtifacts({
    ...base,
    pathSignature: '/s/a.log|/s/b.log',
  }) === true,
  'new session path must refetch',
)
assert(
  shouldRefetchJobArtifacts({
    ...base,
    lastHadContent: false,
  }) === true,
  'empty first snapshot must not latch',
)
assert(
  shouldRefetchJobArtifacts({
    ...base,
    lastHadContent: false,
    pathSignature: '',
    lastPathSignature: '',
  }) === false,
  'still no paths — nothing to read yet',
)

// A→B navigation: request was for A. Closed-over jobId is still A (the bug).
// Current route is B. Must drop A's payload.
assert(
  acceptJobArtifactsResponse('job_aaa', 'job_aaa') === true,
  'same job keeps its own artifacts',
)
assert(
  acceptJobArtifactsResponse('job_aaa', 'job_bbb') === false,
  'late A response must not apply on B',
)
assert(
  acceptJobArtifactsResponse('job_aaa', '  job_bbb  ') === false,
  'trim does not make a different job match',
)
assert(acceptJobArtifactsResponse('', 'job_bbb') === false, 'empty request')
assert(acceptJobArtifactsResponse('job_aaa', '') === false, 'empty route')

const closedOverRoute = 'job_aaa'
const routeNow = 'job_bbb'
const requestId = 'job_aaa'
assert(
  acceptJobArtifactsResponse(requestId, closedOverRoute) === true,
  'stale closure (jobId at fetch start) would wrongly accept A on B',
)
assert(
  acceptJobArtifactsResponse(requestId, routeNow) === false,
  'current route ref must drop the late A payload',
)

console.log('artifacts.test.ts: ok')
