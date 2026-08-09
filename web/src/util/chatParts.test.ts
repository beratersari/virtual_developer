/**
 * Run: npx tsx src/util/chatParts.test.ts
 */
import type { ChatMessage } from '../api/types'
import { extractThinkFromText, groupChatMessages, normalizeChatParts } from './chatParts'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const think = extractThinkFromText('hello <think>secret plan</think> world')
assert(think.thinking === 'secret plan', 'extract think inner')
assert(think.rest.includes('hello') && think.rest.includes('world'), 'extract rest has hello world')

const parts = normalizeChatParts([
  { id: 's', type: 'step-start' },
  { id: 'r1', type: 'reasoning', text: 'look around' },
  { id: 'r2', type: 'thinking', text: 'then edit' },
  { id: 't', type: 'tool', tool: 'bash', status: 'completed' },
  { id: 'x', type: 'text', text: 'Done.\n<think>hidden</think>' },
  { id: 'f', type: 'step-finish' },
])
assert(
  parts.map((p) => p.type).join(',') === 'reasoning,tool,reasoning,text',
  `normalize order ${parts.map((p) => p.type)}`,
)
assert(parts[0].text?.includes('look around') && parts[0].text?.includes('then edit'), 'merge consecutive thinking')
assert(parts[2].text === 'hidden', 'think tag becomes reasoning')
assert(parts[3].text === 'Done.', 'text without think tag')

const msgs: ChatMessage[] = [
  { id: 'u', session_id: 'ses_a', role: 'user', parts: [{ id: 'u1', type: 'text', text: 'go' }] },
  {
    id: 'a1',
    session_id: 'ses_a',
    role: 'assistant',
    agent: 'Atlas',
    parts: [
      { id: 'r', type: 'reasoning', text: 'step 1 think' },
      { id: 'tb', type: 'tool', tool: 'read' },
    ],
  },
  {
    id: 'a2',
    session_id: 'ses_a',
    role: 'assistant',
    parts: [
      { id: 'r2', type: 'reasoning', text: 'step 2 think' },
      { id: 'tx', type: 'text', text: 'answer' },
    ],
  },
  { id: 'u2', session_id: 'ses_a', role: 'user', parts: [{ id: 'u2p', type: 'text', text: 'again' }] },
]
const groups = groupChatMessages(msgs)
assert(groups.length === 3, `group count ${groups.length}`)
assert(groups[0].role === 'user' && groups[1].role === 'assistant' && groups[2].role === 'user', 'roles')
assert(groups[1].parts.filter((p) => p.type === 'reasoning').length === 2, 'thinking stays in order not dumped first')
assert(groups[1].parts.map((p) => p.type).join(',') === 'reasoning,tool,reasoning,text', 'assistant turn chronological')

console.log('chatParts.test.ts ok')
