/**
 * Run: npx tsx src/api/pageLoad.test.ts
 *
 * A live tick must not abort a GET that is already running, and it must not
 * start a second one. Leaving the page aborts the GET so the next page can
 * use the browser connection immediately.
 */
import { createPageLoad, noteLiveGeneration } from './pageLoad'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

function requiredSignal(signal: AbortSignal | null, msg: string): AbortSignal {
  if (!signal) throw new Error(msg)
  return signal
}

{
  const flight = createPageLoad()
  const first = requiredSignal(flight.query('jobs'), 'first query starts')
  assert(!first.aborted, 'first query starts')
  assert(flight.query('jobs') === null, 'same query does not start a second GET')
  assert(!first.aborted, 'same query leaves the in-flight GET alone')
  assert(flight.settle(first) === false, 'a duplicate query does not schedule another GET')
}

{
  const flight = createPageLoad()
  const first = requiredSignal(flight.query('jobs'), 'query signal')
  assert(flight.tick('jobs') === null, 'live tick does not start a second GET')
  assert(!first.aborted, 'live tick does not abort the in-flight GET')
  assert(flight.tick('jobs') === null, 'a second tick still does not start a GET')
  assert(flight.settle(first) === true, 'one follow-up runs after the in-flight GET')
  assert(flight.settle(first) === false, 'the follow-up is only queued once')
  const next = flight.tick('jobs')
  assert(next !== null && !next.aborted, 'the follow-up tick can start once idle')
}

{
  const flight = createPageLoad()
  const first = requiredSignal(flight.query('page-1'), 'page 1')
  const next = flight.query('page-2')
  assert(first.aborted, 'a new query aborts the previous GET')
  assert(next !== null && !next.aborted, 'the new query owns the slot')
  assert(flight.settle(first) === false, 'the aborted GET does not start a follow-up')
}

{
  const flight = createPageLoad()
  const first = requiredSignal(flight.query('storage'), 'storage')
  flight.tick('storage')
  flight.dispose()
  assert(first.aborted, 'leaving the page aborts the GET')
  assert(flight.settle(first) === false, 'leaving the page drops the queued tick')
  const again = flight.query('storage')
  assert(again !== null && !again.aborted, 'a remount can load again')
}

{
  const seen = { current: null as number | null }
  assert(noteLiveGeneration(seen, 0) === false, 'opening the page is not a live tick')
  assert(noteLiveGeneration(seen, 0) === false, 'the same generation does not refetch')
  assert(noteLiveGeneration(seen, 2) === true, 'a later generation refetches')
  assert(noteLiveGeneration(seen, 2) === false, 'that generation is consumed once')
}

console.log('pageLoad.test.ts: ok')
