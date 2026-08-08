/**
 * Lightweight assertions for path matching (run via: npx tsx src/util/paths.test.ts)
 * Kept as a plain script so we don't add a frontend test runner.
 */
import {
  findPromptForJobPath,
  jobSessionPaths,
  normalizePath,
  pathBasename,
  pathStem,
  pathsMatch,
  sessionLogRetryLabel,
  sessionLogSortKey,
} from './paths'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(normalizePath('C:\\a\\b.log') === 'C:/a/b.log', 'normalize windows')
assert(pathBasename('/tmp/KAN-1_x.prompt.txt') === 'KAN-1_x.prompt.txt', 'basename')
assert(pathStem('KAN-1_x.prompt.txt') === 'KAN-1_x', 'stem prompt')
assert(pathStem('KAN-1_x.log') === 'KAN-1_x', 'stem log')
assert(pathsMatch('/a/b/c.log', 'c.log'), 'basename match')
assert(pathsMatch('C:\\sess\\x.log', '/other/x.log'), 'basename cross-os')
assert(
  pathsMatch('/sessions/KAN-1_foo.log', 'o.log') === false,
  'must not treat arbitrary path suffix as the same artifact',
)
assert(
  pathsMatch('/sessions/KAN-10_2024.log', 'KAN-1_2024.log') === false,
  'KAN-10 must not match KAN-1 basename',
)

const prompts = [
  { path: '/sessions/KAN-1_a.prompt.txt', content: 'A' },
  { path: '/sessions/KAN-1_b.prompt.txt', content: 'B' },
]
const hit = findPromptForJobPath(
  prompts,
  '/sessions/KAN-1_b.prompt.txt',
  null,
)
assert(hit?.content === 'B', 'direct prompt path')

const viaLog = findPromptForJobPath(prompts, null, '/sessions/KAN-1_a.log')
assert(viaLog?.content === 'A', 'prompt via session log stem')

const miss = findPromptForJobPath(prompts, '/sessions/missing.prompt.txt', null)
assert(miss === undefined, 'no fail-open match')

assert(sessionLogRetryLabel('KAN-1_20260101_120000.log') === 'initial', 'initial label')
assert(sessionLogRetryLabel('KAN-1_20260101_120100_retry1.log') === 'retry1', 'retry1 label')
assert(sessionLogRetryLabel('KAN-1_20260101_120200_retry2.log') === 'retry2', 'retry2 label')
assert(sessionLogSortKey('x_retry2.log') > sessionLogSortKey('x_retry1.log'), 'sort retry')

const nested = jobSessionPaths({
  session_log_path: '/s/final_retry1.log',
  session_log_paths: ['/s/first.log'],
  retry_attempts: [{ failed_session_log_path: '/s/first.log' }],
})
assert(nested.includes('/s/first.log') && nested.includes('/s/final_retry1.log'), 'job paths nest retries')

console.log('paths.test.ts: ok')
