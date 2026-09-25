/**
 * Run: npx tsx src/util/claudeReviewA104286.test.ts
 */
import { claudeTranscriptEventsFromLog } from './claudeLog'

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

const plain = [
  'The call failed with {',
  '"error": "nope"',
  '}',
  'Retry with the default model.',
].join('\n')
const plainEvents = claudeTranscriptEventsFromLog(plain)
const plainBlob = plainEvents.map((ev) => ev.body || '').join('\n')
assert(
  plainBlob.includes('Retry with the default model.'),
  `plain reply after a multi-line JSON example must stay, got ${JSON.stringify(plainEvents)}`,
)

if (failures.length) {
  throw new Error(failures.join('\n'))
}

console.log('claudeReviewA104286.test.ts ok')
