/**
 * Daily-usage proofs for the Claude transcript.
 * Run: npx tsx src/util/claudeReviewCurrent.test.ts
 */
import React, { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { LiveContext, type LiveValue } from '../app/live'
import { claudeTranscriptEventsFromLog } from './claudeLog'

;(globalThis as unknown as { React: typeof React }).React = React
const { JobChatTab } = await import('../pages/jobs/JobChatTab')

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

const reply = [
  'The call failed with {',
  '"error": "nope"',
  '}',
  'Retry with the default model.',
].join('\n')

const events = claudeTranscriptEventsFromLog(reply)
const body = events.map((ev) => ev.body || '').join('\n')
assert(body.includes('Retry with the default model.'), 'text after a leaked error object is kept')
assert(!body.includes('"error"'), 'raw error json is not shown')

const nested = [
  JSON.stringify({
    type: 'assistant',
    message: {
      role: 'assistant',
      content: [{ type: 'tool_use', name: 'Bash', input: { command: 'pytest -q' } }],
    },
  }),
  JSON.stringify({
    type: 'user',
    message: {
      role: 'user',
      content: [
        {
          type: 'tool_result',
          is_error: true,
          content: JSON.stringify({
            type: 'test_report',
            error: { message: 'FAILED tests/test_app.py::test_save AssertionError' },
          }),
        },
      ],
    },
  }),
].join('\n')
const nestedEvents = claudeTranscriptEventsFromLog(nested)
assert(
  nestedEvents.some((ev) => (ev.body || '').includes('FAILED tests/test_app.py::test_save')),
  'a failed tool JSON body keeps the assertion text',
)
assert(
  nestedEvents.some((ev) => ev.kind === 'error'),
  'a failed tool result is an error row',
)

const tools = [
  JSON.stringify({
    type: 'assistant',
    message: {
      role: 'assistant',
      content: [
        { type: 'tool_use', name: 'Bash', input: { command: 'ls' } },
        { type: 'tool_use', name: 'Read', input: { file_path: 'a.ts' } },
      ],
    },
  }),
  JSON.stringify({
    type: 'user',
    message: {
      role: 'user',
      content: [
        { type: 'tool_result', is_error: true, content: 'ls failed' },
        { type: 'tool_result', is_error: false, content: 'file text' },
      ],
    },
  }),
].join('\n')

const live: LiveValue = {
  connected: false,
  meta: null,
  poll: null,
  settings: null,
  generation: 0,
  pollCountdown: null,
  error: null,
  queueQueued: 0,
  setSettings: () => {},
}

const html = renderToStaticMarkup(
  createElement(
    LiveContext.Provider,
    { value: live },
    createElement(JobChatTab, {
      jobId: 'job-1',
      worker: 'claude',
      sessionLogs: [{ path: 'session.jsonl', name: 'session.jsonl', content: tools, truncated: false }],
      prompts: [],
    }),
  ),
)

const bashAt = html.indexOf('Bash')
const readAt = html.indexOf('Read')
assert(bashAt >= 0 && readAt > bashAt, 'both tool calls render')
const bashSlice = html.slice(bashAt, readAt)
const readSlice = html.slice(readAt)
assert(bashSlice.includes('ls failed'), 'the failed Bash call shows its own error')
assert(bashSlice.includes('error'), 'the failed Bash call is marked error')
assert(readSlice.includes('file text'), 'Read shows its own output')
assert(!readSlice.includes('ls failed'), 'Read does not show the Bash error')

if (failures.length) {
  throw new Error(failures.join('\n'))
}
