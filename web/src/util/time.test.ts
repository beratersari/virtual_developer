/**
 * Lightweight assertions (run: npx tsx src/util/time.test.ts)
 */
import {
  elapsedSecondsBetween,
  formatElapsedBetween,
  formatElapsedSeconds,
  parseTimeMs,
} from './time'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(parseTimeMs(null) === null, 'null parse')
assert(parseTimeMs('') === null, 'empty parse')
assert(parseTimeMs('not-a-date') === null, 'invalid parse')

const start = '2026-01-01T12:00:00.000Z'
const end = '2026-01-01T13:02:05.000Z'
assert(elapsedSecondsBetween(start, end) === 3725, 'elapsed fixed window')
assert(formatElapsedSeconds(3725) === '1h 02m 05s', 'format hms')
assert(formatElapsedSeconds(65) === '1m 05s', 'format ms')
assert(formatElapsedSeconds(9) === '9s', 'format s')
assert(formatElapsedSeconds(null) === '—', 'null format')
assert(formatElapsedBetween(null, end) === '—', 'no start')
assert(formatElapsedBetween(start, end) === '1h 02m 05s', 'between')

const now = Date.parse(start) + 30_000
assert(elapsedSecondsBetween(start, null, now) === 30, 'running uses now')
assert(formatElapsedBetween(start, null, now) === '30s', 'running format')

console.log('time.test.ts: ok')
