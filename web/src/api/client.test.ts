/**
 * Run: npx tsx src/api/client.test.ts
 */
import {
  ANALYTICS_TIMEOUT_MS,
  DEFAULT_GET_TIMEOUT_MS,
  isAbortError,
} from './client'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(DEFAULT_GET_TIMEOUT_MS === 15_000, 'cheap GETs stay on the 15s budget')
assert(ANALYTICS_TIMEOUT_MS === 60_000, 'Analytics GET budget is 60s, not 15s')
assert(ANALYTICS_TIMEOUT_MS > DEFAULT_GET_TIMEOUT_MS, 'Analytics outlives default GET abort')

const abort = new DOMException('Aborted', 'AbortError')
assert(isAbortError(abort), 'DOM AbortError is an abort')
assert(isAbortError({ name: 'AbortError' }), 'plain AbortError is an abort')
assert(!isAbortError(new Error('Request timed out')), 'timeout Error is not an abort')
assert(!isAbortError(null), 'null is not an abort')

console.log('client.test.ts: ok')
