/**
 * Run: npx tsx src/util/modelPicker.test.ts
 */
import { CUSTOM_MODEL, modelSelectValue } from './modelPicker'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const known = ['opencode/hy3-free', 'opencode/other']

assert(modelSelectValue('', known, false) === '', 'empty stays default')
assert(
  modelSelectValue('opencode/hy3-free', known, false) === 'opencode/hy3-free',
  'listed id',
)
assert(modelSelectValue('', known, true) === CUSTOM_MODEL, 'custom flag stays')
assert(
  modelSelectValue('gpt-5', known, false) === CUSTOM_MODEL,
  'unknown id is custom',
)
assert(
  modelSelectValue('opencode/hy3-free', known, true) === CUSTOM_MODEL,
  'custom wins over listed',
)

console.log('modelPicker.test.ts ok')
