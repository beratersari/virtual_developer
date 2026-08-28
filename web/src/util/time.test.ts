/**
 * Run: npx tsx src/lib/time.test.ts
 */
import {
  datetimeLocalToNaiveIso,
  formatScheduleWhen,
  joinDatetimeLocal,
  localNaiveNowIso,
  elapsedSecondsBetween,
  formatChatTime,
  formatDashboardClock,
  formatElapsedBetween,
  formatElapsedSeconds,
  normalizeTime24h,
  parseTimeMs,
  splitDatetimeLocal,
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

assert(datetimeLocalToNaiveIso('2026-08-08T15:29') === '2026-08-08T15:29:00', 'local pad seconds')
assert(datetimeLocalToNaiveIso('2026-08-08T15:29:00') === '2026-08-08T15:29:00', 'already naive')
assert(datetimeLocalToNaiveIso('') === '', 'empty local')
const fixed = new Date(2026, 7, 14, 14, 21, 50)
assert(localNaiveNowIso(fixed) === '2026-08-14T14:21:50', 'naive now is local wall clock')
const naive = localNaiveNowIso()
assert(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(naive), 'naive now shape')
assert(!naive.endsWith('Z'), 'naive now has no Z')
assert(splitDatetimeLocal('2026-08-28T14:30').date === '2026-08-28', 'split date')
assert(splitDatetimeLocal('2026-08-28T14:30').time === '14:30', 'split 24h time')
assert(normalizeTime24h('9:05') === '09:05', 'pad hour')
assert(normalizeTime24h('23:59') === '23:59', 'late evening')
assert(normalizeTime24h('24:00') === '', 'reject 24:00')
assert(normalizeTime24h('2:30 PM') === '', 'reject am/pm')
assert(joinDatetimeLocal('2026-08-28', '14:30') === '2026-08-28T14:30', 'join')
assert(formatScheduleWhen('2026-08-28T14:30:00') === '2026-08-28 14:30', 'list 24h')
assert(!formatScheduleWhen('2026-08-28T14:30:00').toLowerCase().includes('pm'), 'no pm')
assert(!formatScheduleWhen('2026-08-28T14:30:00').toLowerCase().includes('am'), 'no am')
assert(formatElapsedBetween(start, null, now) === '30s', 'running format')

const chatLabel = formatChatTime('2026-08-09T16:04:18+00:00')
assert(chatLabel.length > 8, `chat time formatted: ${chatLabel}`)
const clock = formatDashboardClock(Date.parse('2026-08-10T12:30:00Z'))
assert(clock.length > 8, `dashboard clock formatted: ${clock}`)
assert(!chatLabel.includes('+00:00'), 'chat time is local, not raw UTC offset')

console.log('time.test.ts: ok')
