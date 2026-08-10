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

const compactMsgs: ChatMessage[] = [
  {
    id: 'c1',
    session_id: 'ses_a',
    role: 'user',
    parts: [{ id: 'cp', type: 'compaction', auto: true, text: 'Session compacted' }],
  },
  {
    id: 'c2',
    session_id: 'ses_a',
    role: 'user',
    parts: [
      { id: 'cp2', type: 'compaction', auto: true },
      { id: 'ct2', type: 'text', text: 'Session compacted to free context.' },
    ],
  },
  {
    id: 'c3',
    session_id: 'ses_a',
    role: 'assistant',
    agent: 'compaction',
    summary: true,
    parts: [{ id: 'sum', type: 'text', text: '## Compaction summary\nWork so far…' }],
  },
  {
    id: 'cont',
    session_id: 'ses_a',
    role: 'user',
    parts: [
      {
        id: 'ct',
        type: 'text',
        text: 'Continue the previous OpenCode session. The last turn stopped early',
      },
    ],
  },
  {
    id: 'restore',
    session_id: 'ses_a',
    role: 'user',
    parts: [
      {
        id: 'rst',
        type: 'text',
        text: '[restore checkpointed session agent configuration after compaction]\n<!-- OMO_INTERNAL_INITIATOR -->',
      },
    ],
  },
  {
    id: 'occont',
    session_id: 'ses_a',
    role: 'user',
    parts: [
      {
        id: 'occ',
        type: 'text',
        text: 'Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.',
      },
    ],
  },
  { id: 'u3', session_id: 'ses_a', role: 'user', parts: [{ id: 'u3p', type: 'text', text: 'real ask' }] },
]
const compactGroups = groupChatMessages(compactMsgs)
assert(compactGroups.length === 4, `compact groups ${compactGroups.length}`)
assert(compactGroups.every((g) => g.role !== 'user' || g.parts[0].text === 'real ask'), 'compact is not You')
assert(compactGroups[0].role === 'compaction', 'compact part is not You')
assert(compactGroups[1].role === 'compaction', 'compact+text is not You')
assert(compactGroups[2].role === 'compaction', 'summary assistant is compact chip')
assert(compactGroups[3].role === 'user' && compactGroups[3].parts[0].text === 'real ask', 'continue prompt hidden')

console.log('chatParts.test.ts ok')
