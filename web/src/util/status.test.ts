/**
 * Run: npx tsx src/util/status.test.ts
 */
import { jobIsCancellable, jobIsDeletable } from './status'

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

console.log('status.test.ts: ok')
