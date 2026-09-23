/**
 * Run: npx tsx src/util/claudeLog.test.ts
 */
import { buildClaudeTranscriptEvents, claudeDisplayText } from './claudeLog'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const KAN537 =
  'YaverFreeOk\nHere’s a quick check for you.\n[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}'

assert(
  claudeDisplayText(KAN537) === 'YaverFreeOk\nHere’s a quick check for you.',
  'live KAN-537 log',
)

assert(
  claudeDisplayText(
    'Hello [claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"} there',
  ) === 'Hello there',
  'diagnostic on the same line',
)

assert(
  !claudeDisplayText(
    '[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk","extra":{"nested":true}}',
  ).includes('query_source'),
  'nested diagnostic json is dropped',
)

assert(
  claudeDisplayText(
    '\u001b[33m[claude-code:unrecognized_model]\u001b[0m {"model":"openai-fast","query_source":"sdk"}\nYaverFreeOk',
  ) === 'YaverFreeOk',
  'ansi wrapped diagnostic',
)

const envelope = JSON.stringify({
  type: 'result',
  subtype: 'success',
  is_error: false,
  result: 'YaverFreeOk\nHere’s a quick check for you.',
  session_id: 'd8981004-374f-4ba4-97cc-ae56063695d6',
  usage: { input_tokens: 3, output_tokens: 4 },
  modelUsage: { 'openai-fast': { outputTokens: 4 } },
})
assert(
  claudeDisplayText(envelope + '\n' + '[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}') ===
    'YaverFreeOk\nHere’s a quick check for you.',
  'json result plus stderr diagnostic',
)

const pretty = `{
  "type": "result",
  "is_error": false,
  "result": "From pretty JSON.",
  "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
}`
assert(claudeDisplayText(pretty) === 'From pretty JSON.', 'pretty json')

const stream = [
  JSON.stringify({
    type: 'system',
    subtype: 'init',
    session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    model: 'openai-fast',
  }),
  JSON.stringify({
    type: 'assistant',
    message: {
      role: 'assistant',
      content: [
        { type: 'text', text: 'Working.' },
        { type: 'tool_use', name: 'Bash', input: { command: 'ls' } },
      ],
    },
  }),
  JSON.stringify({
    type: 'result',
    subtype: 'success',
    is_error: false,
    result: 'Working.',
    session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    total_cost_usd: 0,
  }),
].join('\n')
assert(claudeDisplayText(stream) === 'Working.', 'stream-json keeps the result only')
assert(!claudeDisplayText(stream).includes('tool_use'), 'stream-json hides tool json')
assert(!claudeDisplayText(stream).includes('total_cost_usd'), 'stream-json hides usage')

const blocks = JSON.stringify({
  type: 'result',
  result: [{ type: 'text', text: 'From blocks.' }],
  session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
})
assert(claudeDisplayText(blocks) === 'From blocks.', 'result content blocks')

assert(
  claudeDisplayText(
    JSON.stringify({
      type: 'result',
      is_error: true,
      result: '',
      error: 'model not found',
      session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    }),
  ) === 'model not found',
  'error result',
)

assert(
  claudeDisplayText('Use this config:\n{"ok": true, "name": "yaver"}') ===
    'Use this config:\n{"ok": true, "name": "yaver"}',
  'the model’s own json stays',
)

assert(claudeDisplayText('YaverFreeOk\r\nSecond line.\r\n') === 'YaverFreeOk\nSecond line.', 'crlf')

assert(
  claudeDisplayText('[claude-code:unrecognized_model] {"model":"openai-fast","query_source":"sdk"}') === '',
  'diagnostic only is empty',
)

const events = buildClaudeTranscriptEvents(
  [{ path: 'KAN-537.log', content: KAN537, truncated: false }],
  [
    {
      path: 'KAN-537.prompt.txt',
      content: '# derman-build job (Yaver)\n\nOpenCode agent: **derman-build**. Do the work.',
      truncated: false,
    },
  ],
)
assert(events.length === 2, 'prompt and reply')
assert(events[0].kind === 'user', 'user')
assert(events[0].body?.includes('Agent: **derman-build**'), 'prompt is not labeled OpenCode')
assert(!events[0].body?.includes('OpenCode agent:'), 'old header rewritten')
assert(events[1].title === 'Claude Code', 'title')
assert(events[1].body === 'YaverFreeOk\nHere’s a quick check for you.', 'reply')
assert(!JSON.stringify(events).includes('query_source'), 'transcript has no diagnostic json')
assert(!JSON.stringify(events).includes('unrecognized_model'), 'transcript has no warning tag')

console.log('claudeLog.test.ts ok')
