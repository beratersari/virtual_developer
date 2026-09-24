/**
 * Run: npx tsx src/util/claudeUserTurn.test.ts
 */
import { claudeTranscriptEventsFromLog } from './claudeLog'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const log = JSON.stringify({
  type: 'user',
  message: { role: 'user', content: [{ type: 'text', text: 'Please continue.' }] },
})
const events = claudeTranscriptEventsFromLog(log)
const turn = events.find((ev) => ev.body === 'Please continue.')
assert(turn != null, 'user text is kept')
assert(
  turn?.kind === 'user' && turn.title === 'You',
  `user turn must render as You, got kind=${turn?.kind} title=${turn?.title}`,
)

console.log('claudeUserTurn.test.ts ok')
