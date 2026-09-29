import { rowsFromRepositorySet } from './MoreRepositories'
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
console.log('repo rows ok')
