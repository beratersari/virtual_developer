/**
 * Lightweight assertions for path matching (run via: npx tsx src/util/paths.test.ts)
 * Kept as a plain script so we don't add a frontend test runner.
 */
import {
  findPromptForJobPath,
  normalizePath,
  pathBasename,
  pathStem,
  pathsMatch,
} from './paths'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(normalizePath('C:\\a\\b.log') === 'C:/a/b.log', 'normalize windows')
assert(pathBasename('/tmp/KAN-1_x.prompt.txt') === 'KAN-1_x.prompt.txt', 'basename')
assert(pathStem('KAN-1_x.prompt.txt') === 'KAN-1_x', 'stem prompt')
assert(pathStem('KAN-1_x.log') === 'KAN-1_x', 'stem log')
assert(pathsMatch('/a/b/c.log', 'c.log'), 'suffix match')
assert(pathsMatch('C:\\sess\\x.log', '/other/x.log'), 'basename cross-os')

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

console.log('paths.test.ts: ok')
