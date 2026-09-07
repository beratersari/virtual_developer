/**
 * Run: npx tsx src/util/liveTick.test.ts
 */
import { liveDataSignature, shouldBumpLiveGeneration } from './liveTick'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(liveDataSignature({ live_issue_keys: ['b', 'a'], queue: { queued_count: 2 } }) === 'A,B|2', 'sig')

const pollOnly = shouldBumpLiveGeneration(
  { type: 'live', live_issue_keys: ['KAN-1'], queue: { queued_count: 0 } },
  'KAN-1|0',
)
assert(pollOnly.bump === false, 'same live set does not refetch')

const started = shouldBumpLiveGeneration(
  { type: 'live', live_issue_keys: ['KAN-1', 'KAN-2'], queue: { queued_count: 0 } },
  'KAN-1|0',
)
assert(started.bump === true, 'new live issue refetches')

const full = shouldBumpLiveGeneration({ type: 'dashboard', jobs: [] }, 'KAN-1|0')
assert(full.bump === true, 'legacy full envelope still refetches')

console.log('liveTick.test.ts: ok')
