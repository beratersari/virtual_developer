/**
 * Run: npx tsx src/pages/settings/savedProjects.test.ts
 */
import {
  editorIndexAfterRemoval,
  filterSavedProjects,
  savedProjectMatchesName,
  savedProjectName,
  toggleVisibleSelection,
  visibleSelectionState,
  withoutSelectedProjects,
} from './savedProjects'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const orders = {
  label: 'Orders',
  url: 'https://gitlab.com/acme/billing.git',
  target_branch: 'main',
}
const blank = {
  label: '  ',
  url: 'https://gitlab.com/acme/ledger.git',
  target_branch: 'develop',
}
const untitled = { label: '', url: '', target_branch: '' }
const projects = [orders, blank, untitled]

assert(savedProjectName(orders) === 'Orders', 'a label is the project name')
assert(
  savedProjectName(blank) === 'https://gitlab.com/acme/ledger.git',
  'a blank label uses the URL as the name',
)
assert(savedProjectName(untitled) === 'Untitled project', 'an empty row is untitled')

assert(savedProjectMatchesName(orders, 'ord'), 'name search is case-insensitive')
assert(savedProjectMatchesName(orders, '  ORDERS '), 'surrounding spaces are ignored')
assert(
  !savedProjectMatchesName(orders, 'billing'),
  'a named project does not match its URL',
)
assert(savedProjectMatchesName(blank, 'ledger'), 'a blank label can match the URL')
assert(savedProjectMatchesName(untitled, 'untitled'), 'untitled matches its visible name')
assert(savedProjectMatchesName(orders, '   '), 'an empty search keeps every project')

const named = filterSavedProjects(projects, 'orders')
assert(named.length === 1 && named[0].index === 0, 'name filter keeps the original index')
assert(filterSavedProjects(projects, 'missing').length === 0, 'an unknown name matches nothing')
assert(filterSavedProjects(projects, '').length === 3, 'no query returns the full list')

const ordersKey = named[0].key
const ledgerKey = filterSavedProjects(projects, 'ledger')[0].key
let selected = toggleVisibleSelection([ordersKey], new Set<string>(), true)
assert(selected.has(ordersKey) && !selected.has(ledgerKey), 'select all adds only visible rows')
selected = toggleVisibleSelection([ordersKey], new Set([ordersKey, ledgerKey]), false)
assert(
  !selected.has(ordersKey) && selected.has(ledgerKey),
  'clearing select all keeps rows hidden by the search',
)
assert(
  visibleSelectionState([ordersKey, ledgerKey], new Set([ordersKey])) === 'some',
  'a partial visible selection is indeterminate',
)
assert(
  visibleSelectionState([ordersKey], new Set([ordersKey, ledgerKey])) === 'all',
  'every visible row selected is select-all',
)
assert(visibleSelectionState([], new Set([ordersKey])) === 'none', 'no visible rows is not select-all')

const removed = withoutSelectedProjects(projects, new Set([ordersKey, ledgerKey]))
assert(removed.length === 1 && removed[0] === untitled, 'bulk delete drops only the selection')
assert(projects.length === 3, 'bulk delete does not mutate the original list')
assert(
  editorIndexAfterRemoval(0, projects, new Set([ordersKey])) === null,
  'deleting the open project closes its editor',
)
assert(
  editorIndexAfterRemoval(1, projects, new Set([ordersKey])) === 0,
  'a later editor index shifts when earlier rows are deleted',
)
assert(
  editorIndexAfterRemoval(2, projects, new Set([ordersKey, ledgerKey])) === 0,
  'the editor follows a row that stays',
)
assert(editorIndexAfterRemoval(null, projects, new Set([ordersKey])) === null, 'a new project stays new')

console.log('savedProjects.test.ts: ok')
