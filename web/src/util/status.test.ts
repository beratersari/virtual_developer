/**
 * Run: npx tsx src/lib/status.test.ts
 */
import { jobIsCancellable, jobIsDeletable, jobMatchesFilter } from './status'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(jobIsCancellable('executing', true) === true, 'live executing cancellable')
assert(jobIsCancellable('running', false) === true, 'running status cancellable')
assert(jobIsCancellable('planning', false) === true, 'planning cancellable')
assert(jobIsCancellable('pending', false) === true, 'pending cancellable')
assert(jobIsCancellable('completed', false) === false, 'completed not cancellable')
assert(jobIsCancellable('error', false) === false, 'error not cancellable')
assert(jobIsCancellable('cancelled', false) === false, 'cancelled not cancellable')
assert(jobIsCancellable('completed', true) === false, 'completed not cancellable even if live flag')

assert(jobIsDeletable('completed', false) === true, 'completed deletable')
assert(jobIsDeletable('executing', false) === false, 'executing not deletable')
assert(jobIsDeletable('completed', true) === false, 'live not deletable')

assert(jobMatchesFilter('executing', true, 'active') === true, 'live executing is in flight')
assert(jobMatchesFilter('executing', false, 'active') === true, 'stuck executing is in flight')
assert(jobMatchesFilter('completed', false, 'active') === false, 'completed not in flight')
assert(
  jobMatchesFilter('executing', false, 'live') === jobMatchesFilter('executing', false, 'active'),
  'live filter matches active (one In flight tab)',
)

console.log('status.test.ts: ok')
