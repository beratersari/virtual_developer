/**
 * Run: npx tsx src/util/jiraMentions.test.ts
 */
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { JiraLinkedText } from '../ui/JiraLinkedText'
import { jiraBrowseUrl, linkJiraMentions } from './jiraMentions'

;(globalThis as unknown as { React: typeof React }).React = React

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const host = 'https://jira.example.com/'

assert(
  jiraBrowseUrl(host, 'kan-12') === 'https://jira.example.com/browse/KAN-12',
  'browse url drops a trailing slash and uppercases the key',
)

const linked = linkJiraMentions('See KAN-12 and PROJ-3.', host)
assert(linked.filter((part) => part.href).length === 2, 'each mentioned key is a link')
assert(linked[1]?.href === 'https://jira.example.com/browse/KAN-12', 'first key')
assert(linked[3]?.href === 'https://jira.example.com/browse/PROJ-3', 'second key')

const titled = linkJiraMentions('feat(KAN-9): login', host)
assert(titled.some((part) => part.text === 'KAN-9' && part.href?.endsWith('/browse/KAN-9')), 'key inside a title')

const already = linkJiraMentions('Open https://jira.example.com/browse/KAN-4 now', host)
assert(!already.some((part) => part.href), 'a key already inside a URL stays text')

const synthetic = linkJiraMentions('GitLab GL-15 and Azure AZ-8 plus KAN-1', host)
assert(synthetic.filter((part) => part.href).map((part) => part.text).join() === 'KAN-1', 'GL and AZ ids are not Jira')

assert(linkJiraMentions('KAN-2', '').every((part) => !part.href), 'no host means no links')

const html = renderToStaticMarkup(
  React.createElement(JiraLinkedText, {
    text: 'Blocked by KAN-8.',
    jiraHost: 'https://jira.example.com',
  }),
)
assert(html.includes('href="https://jira.example.com/browse/KAN-8"'), 'title or description renders a browse link')
assert(html.includes('target="_blank"'), 'the ticket opens in a new tab')
assert(html.includes('>KAN-8<'), 'the visible text stays the key')

console.log('jiraMentions.test.ts ok')
