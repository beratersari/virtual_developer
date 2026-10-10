/**
 * Run: npx tsx src/pages/settings/settingsNumbers.test.ts
 */
import {
  parseSettingsNumber,
  requireSettingsNumber,
  settingsNumberProblem,
} from './settingsNumbers'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(parseSettingsNumber('') === null, 'a blank field is not zero')
assert(parseSettingsNumber('   ') === null, 'spaces are not zero')
assert(parseSettingsNumber('0') === 0, 'a typed zero stays zero')
assert(parseSettingsNumber('15') === 15, 'a positive value under 30 stays')
assert(parseSettingsNumber('nope') === null, 'text is not a number')

assert(
  settingsNumberProblem('temp_clone_max_age_days', 0) === null,
  'clone age may be zero',
)
assert(
  settingsNumberProblem('agent_task_max_retries', 0) === null,
  'retries may be zero',
)
assert(
  settingsNumberProblem('poll_interval_seconds', null) ===
    'Poll interval needs a number.',
  'a blank poll interval is rejected',
)
assert(
  settingsNumberProblem('agent_task_timeout_seconds', 0) !== null,
  'timeout zero is outside the form range',
)

let threw = false
try {
  requireSettingsNumber('max_concurrent_jobs', null)
} catch (e) {
  threw = e instanceof Error && e.message.includes('needs a number')
}
assert(threw, 'save refuses a blank concurrent-jobs value')
assert(requireSettingsNumber('temp_clone_max_age_days', 0) === 0, 'zero is saved')

console.log('settingsNumbers.test.ts: ok')
