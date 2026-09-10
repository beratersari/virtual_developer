/**
 * Run: npx tsx src/util/sessions.test.ts
 */
import { groupSessionBinds, sessionKindGroup } from './sessions'
import type { OpencodeSessionBind } from '../api/types'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

function bind(kind: string, id: string): OpencodeSessionBind {
  return {
    bind_id: id,
    repository_url: 'https://gitlab.example/r.git',
    branch: 'feature/x',
    target_branch: 'main',
    session_id: `ses_${id}`,
    kind,
  }
}

assert(sessionKindGroup('plan') === 'plan', 'plan')
assert(sessionKindGroup('derman-plan') === 'plan', 'derman-plan')
assert(sessionKindGroup('build') === 'build', 'build')
assert(sessionKindGroup('execution') === 'build', 'execution')
assert(sessionKindGroup('test') === 'test', 'test')
assert(sessionKindGroup('derman-test') === 'test', 'derman-test')
assert(sessionKindGroup('') === 'other', 'empty is other')
assert(sessionKindGroup(undefined) === 'other', 'missing is other')

const grouped = groupSessionBinds([
  bind('build', 'b'),
  bind('plan', 'p'),
  bind('test', 't'),
  bind('', 'legacy'),
])
assert(grouped.plan.map((r) => r.bind_id).join() === 'p', 'plan group')
assert(grouped.build.map((r) => r.bind_id).join() === 'b', 'build group')
assert(grouped.test.map((r) => r.bind_id).join() === 't', 'test group')
assert(grouped.other.map((r) => r.bind_id).join() === 'legacy', 'other group')

console.log('sessions.test.ts: ok')
