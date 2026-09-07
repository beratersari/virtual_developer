/**
 * Run: npx tsx src/util/codexLog.test.ts
 */
import { buildCodexTranscriptEvents, parseCodexExecLog } from './codexLog'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const assistantOnly = [
  JSON.stringify({ type: 'thread.started', thread_id: 'tid-1' }),
  JSON.stringify({
    type: 'item.completed',
    item: { type: 'agent_message', text: 'I will implement the change.' },
  }),
  JSON.stringify({
    type: 'item.completed',
    item: { type: 'command_execution', command: 'ls', aggregated_output: 'src' },
  }),
].join('\n')

const noPrompt = parseCodexExecLog(assistantOnly)
assert(noPrompt.every((e) => e.kind !== 'user'), 'exec JSONL has no user turn')
assert(noPrompt.some((e) => e.kind === 'message' && e.body?.includes('implement')), 'assistant')

const withPrompt = parseCodexExecLog(assistantOnly, { userPrompt: 'Add retry guard' })
assert(withPrompt[0]?.kind === 'user', 'operator prompt is first')
assert(withPrompt[0]?.body === 'Add retry guard', 'operator prompt body')
assert(withPrompt.some((e) => e.kind === 'message'), 'assistant still present')

const jsonlUser = parseCodexExecLog(
  JSON.stringify({
    type: 'item.completed',
    item: { type: 'user_message', text: 'second turn' },
  }),
  { userPrompt: 'first turn' },
)
assert(jsonlUser.filter((e) => e.kind === 'user').length === 2, 'jsonl user plus prompt')

const dup = parseCodexExecLog(
  JSON.stringify({
    type: 'item.completed',
    item: { type: 'user_message', text: 'same' },
  }),
  { userPrompt: 'same' },
)
assert(dup.filter((e) => e.kind === 'user').length === 1, 'do not duplicate same prompt')

const arrayContent = parseCodexExecLog(
  JSON.stringify({
    type: 'item.completed',
    item: { type: 'UserMessage', content: [{ type: 'text', text: 'array user' }] },
  }),
)
assert(arrayContent.some((e) => e.kind === 'user' && e.body === 'array user'), 'content array')

const paired = buildCodexTranscriptEvents(
  [{ path: '/sessions/KAN-7_20260823_215414.log', content: assistantOnly }],
  [{ path: '/sessions/KAN-7_20260823_215414.prompt.txt', content: '# Build mode\n\nDo the task' }],
)
assert(paired[0]?.kind === 'user', 'sibling prompt becomes You')
assert(paired[0]?.body?.includes('Do the task'), 'sibling prompt body')
assert(paired.some((e) => e.kind === 'message'), 'paired assistant')

const unmatched = buildCodexTranscriptEvents(
  [{ path: '/sessions/other.log', content: assistantOnly }],
  [{ path: '/sessions/KAN-7_x.prompt.txt', content: 'fallback prompt' }],
)
assert(unmatched[0]?.kind === 'user' && unmatched[0].body === 'fallback prompt', 'unmatched leftover')

console.log('codexLog.test.ts ok')
