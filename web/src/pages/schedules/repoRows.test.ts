import {
  appendRepoRows,
  emptyRepoRow,
  repoDraftProblem,
  repoRowBranches,
  repoRowTitle,
  rowsFromRepositorySet,
  scheduleRepositoryFields,
} from './MoreRepositories'
import { projectMatchesQuery, searchSavedRepos } from '../../ui/ProjectSelect'

const projects = [
  {
    label: 'orders-api',
    url: 'https://gitlab.com/acme/orders-api.git',
    target_branch: 'main',
    source_branch: '',
  },
  {
    label: 'orders-web',
    url: 'https://gitlab.com/acme/orders-web.git',
    target_branch: 'develop',
    source_branch: 'release',
  },
]

if (!projectMatchesQuery(projects[0], 'API')) throw new Error('label search missed')
if (!projectMatchesQuery(projects[1], 'orders-web.git')) throw new Error('url search missed')
if (projectMatchesQuery(projects[0], 'missing')) throw new Error('unrelated query matched')

const rows = rowsFromRepositorySet(
  ['https://gitlab.com/acme/orders-web.git', 'https://gitlab.com/acme/other.git', ''],
  projects,
)
if (rows.length !== 2) throw new Error(`expected 2 rows, got ${rows.length}`)
if (rows[0].url !== projects[1].url || rows[0].target !== 'develop' || rows[0].source !== 'release') {
  throw new Error('saved repository did not keep its branches')
}
if (rows[0].sourceMode !== 'custom') throw new Error('custom source was dropped')
if (rows[1].url !== 'https://gitlab.com/acme/other.git' || rows[1].target !== 'develop') {
  throw new Error('unknown repository was not loaded')
}

const found = searchSavedRepos(projects, 'orders', [projects[0].url])
if (found.pickable.length !== 1 || found.pickable[0].url !== projects[1].url) {
  throw new Error('search hid a saved repository that can still be added')
}
if (found.taken.length !== 1 || found.taken[0].url !== projects[0].url) {
  throw new Error('search dropped a saved repository that is already added')
}
const none = searchSavedRepos(projects, 'missing', [])
if (none.pickable.length !== 0 || none.taken.length !== 0) {
  throw new Error('unrelated query returned a repository')
}

const current = [rows[0]]
const added = appendRepoRows(current, rows)
if (added.added !== 1 || added.rows.length !== 2) {
  throw new Error('a set replaced the repositories already in the list')
}
if (added.rows[0].url !== rows[0].url) {
  throw new Error('adding a set dropped the repository that was already listed')
}
const again = appendRepoRows(added.rows, rows)
if (again.added !== 0 || again.rows.length !== 2) {
  throw new Error('adding the same set inserted a duplicate')
}
const blank = appendRepoRows([emptyRepoRow()], rows)
if (blank.rows.some((row) => !row.url.trim()) || blank.added !== 2) {
  throw new Error('an empty row stayed in the list')
}

if (repoRowTitle(rows[0], projects) !== 'orders-web') {
  throw new Error('list title ignored the saved name')
}
if (repoRowBranches(rows[0]) !== 'release → develop') {
  throw new Error('custom branches were not shown')
}
if (repoRowBranches(rows[1]) !== 'feature/<issue key> → develop') {
  throw new Error('default source was not shown')
}
if (repoRowBranches({ ...emptyRepoRow(), url: 'https://gitlab.com/acme/new.git', sourceMode: 'custom', source: '' }) !== 'custom branch → develop') {
  throw new Error('empty custom source was not labeled')
}
if (repoDraftProblem({ ...emptyRepoRow(), url: rows[0].url }, [rows[0].url]) !== 'That repository is already in the list') {
  throw new Error('duplicate repository was accepted')
}
if (repoDraftProblem({ ...emptyRepoRow(), url: 'https://gitlab.com/acme/new.git', sourceMode: 'custom', source: '' }, []) !== 'Enter a source branch') {
  throw new Error('a custom source branch was optional')
}

const saved = scheduleRepositoryFields([
  { url: 'https://gitlab.com/acme/orders-api.git', source: 'develop', target: 'develop', sourceMode: 'issue_key' },
  { url: 'https://gitlab.com/acme/orders-web.git', source: 'develop', target: 'main', sourceMode: 'issue_key' },
])
if (!saved.repository_refs || saved.repository_refs.length !== 2) {
  throw new Error('two repositories were not sent')
}
if (saved.repository_refs[1].source_branch === 'develop' || saved.repository_refs[1].source_branch !== '') {
  throw new Error('later feature/<issue key> row was saved as develop')
}
if (saved.repository_refs[0].source_branch_mode !== 'issue_key' || saved.repository_refs[1].source_branch_mode !== 'issue_key') {
  throw new Error('feature/<issue key> rows did not mark their source mode')
}
if (saved.repository_refs[1].target_branch !== 'main') {
  throw new Error('later target branch was dropped')
}
const mixed = scheduleRepositoryFields([
  { url: 'https://gitlab.com/acme/orders-api.git', source: 'release', target: 'develop', sourceMode: 'custom' },
  { url: 'https://gitlab.com/acme/orders-web.git', source: 'develop', target: 'main', sourceMode: 'issue_key' },
])
if (mixed.source_branch !== 'release' || mixed.source_branch_mode !== 'custom') {
  throw new Error('first custom branch was not kept as the job source')
}
if (mixed.repository_refs?.[0].source_branch !== 'release' || mixed.repository_refs?.[0].source_branch_mode !== 'custom') {
  throw new Error('custom source was rewritten')
}
if (mixed.repository_refs?.[1].source_branch !== '' || mixed.repository_refs?.[1].source_branch_mode !== 'issue_key') {
  throw new Error('mixed feature/<issue key> row was saved as develop')
}
const flipped = scheduleRepositoryFields([
  { url: 'https://gitlab.com/acme/orders-api.git', source: 'develop', target: 'develop', sourceMode: 'issue_key' },
  { url: 'https://gitlab.com/acme/orders-web.git', source: 'hotfix', target: 'main', sourceMode: 'custom' },
])
if (flipped.repository_refs?.[0].source_branch !== '' || flipped.repository_refs?.[0].source_branch_mode !== 'issue_key') {
  throw new Error('first feature/<issue key> row was saved as develop')
}
if (flipped.repository_refs?.[1].source_branch !== 'hotfix' || flipped.repository_refs?.[1].source_branch_mode !== 'custom') {
  throw new Error('later custom branch was saved as feature/<issue key>')
}
const one = scheduleRepositoryFields([
  { url: 'https://gitlab.com/acme/orders-api.git', source: 'develop', target: 'develop', sourceMode: 'issue_key' },
])
if (one.repository_refs) throw new Error('one repository was sent as a set')
console.log('repo rows ok')
