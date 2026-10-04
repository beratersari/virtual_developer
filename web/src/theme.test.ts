/**
 * Run: npx tsx src/theme.test.ts
 */
import { parseTheme, themeFromStorage, THEME_STORAGE_KEY } from './theme'

const failures: string[] = []

function assert(cond: unknown, msg: string) {
  if (!cond) failures.push(msg)
}

assert(parseTheme(null) === 'dark', 'missing theme stays dark')
assert(parseTheme(undefined) === 'dark', 'unset theme stays dark')
assert(parseTheme('') === 'dark', 'empty theme stays dark')
assert(parseTheme('dark') === 'dark', 'dark is dark')
assert(parseTheme('light') === 'light', 'light is light')
assert(parseTheme('LIGHT') === 'dark', 'theme value is exact')
assert(parseTheme('blue') === 'dark', 'unknown theme stays dark')

assert(
  themeFromStorage(() => null) === 'dark',
  'empty storage stays dark',
)
assert(
  themeFromStorage((key) => (key === THEME_STORAGE_KEY ? 'light' : null)) === 'light',
  'stored light is light',
)
assert(
  themeFromStorage(() => {
    throw new Error('blocked')
  }) === 'dark',
  'a storage error stays dark',
)

if (failures.length) {
  throw new Error(failures.join('\n'))
}
console.log('theme ok')
