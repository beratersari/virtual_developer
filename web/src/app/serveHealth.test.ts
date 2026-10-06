/**
 * Run: npx tsx src/app/serveHealth.test.ts
 */
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ServeHealthMark, serveHealthView } from './ServeHealth'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

function mark(props: {
  status?: string
  message?: string
  healthy?: boolean
  phase?: 'loading' | 'ready' | 'unreachable'
}) {
  return renderToStaticMarkup(createElement(ServeHealthMark, props))
}

const cases: Array<[string, string, string]> = [
  ['ready', 'OpenCode healthy', 'bg-live'],
  ['started', 'OpenCode healthy', 'bg-live'],
  ['reloaded', 'OpenCode healthy', 'bg-live'],
  ['reloading', 'OpenCode reloading', 'bg-warning'],
  ['deferred', 'OpenCode reload waiting', 'bg-warning'],
  ['running', 'OpenCode is up, not answering', 'bg-danger'],
  ['down', 'OpenCode is not running', 'bg-danger'],
  ['failed', 'OpenCode failed', 'bg-danger'],
  ['', 'OpenCode status unavailable', 'bg-danger'],
  ['weird', 'OpenCode status unavailable', 'bg-danger'],
]

for (const [status, label, dot] of cases) {
  const view = serveHealthView(status, false)
  assert(view.label === label, `${status || 'empty'} label is ${view.label}`)
  const html = mark({ status, healthy: false, message: 'detail from serve' })
  assert(html.includes(label), `${status || 'empty'} markup`)
  assert(html.includes(dot), `${status || 'empty'} dot ${html}`)
  assert(html.includes('detail from serve'), `${status || 'empty'} title`)
  assert(html.includes('role="status"'), `${status || 'empty'} status role`)
  assert(html.includes('OpenCode'), `${status || 'empty'} names OpenCode`)
}

const restarting = mark({ status: 'reloading' })
assert(restarting.includes('vd-pulse'), 'a restart pulses')
const waiting = mark({ status: 'deferred', healthy: false })
assert(!waiting.includes('vd-pulse'), 'a waiting reload stays still')
assert(waiting.includes('OpenCode reload waiting'), 'a waiting reload does not claim health')
const waitingHealthy = mark({ status: 'deferred', healthy: true })
assert(
  waitingHealthy.includes('OpenCode healthy, reload waiting'),
  'a healthy serve names the waiting reload',
)
assert(serveHealthView('deferred', true).label === 'OpenCode healthy, reload waiting', 'view')
assert(!waitingHealthy.includes('vd-pulse'), 'a waiting reload stays still when healthy')

const loading = mark({ phase: 'loading' })
assert(loading.includes('Checking OpenCode'), 'first paint checks')
assert(!loading.includes('OpenCode healthy'), 'first paint is not healthy')
assert(loading.includes('bg-text-muted'), 'checking stays quiet')

const missed = mark({ phase: 'unreachable' })
assert(missed.includes('OpenCode status unavailable'), 'a failed read is not healthy')
assert(missed.includes('bg-danger'), 'a failed read uses the down dot')
assert(missed.includes('could not be read'), 'a failed read explains itself')
