import { groupDeliveries, repositoryLabel } from './repoDeliveries'

const api = {
  merge_request_url: 'https://gitlab.com/beratersari0/yaver-orders-api/-/merge_requests/2',
  commit_url: 'https://gitlab.com/beratersari0/yaver-orders-api/-/commit/aaa',
  commit_sha: 'aaa',
  commit_subject: 'api',
}
const web = {
  merge_request_url: 'https://gitlab.com/beratersari0/yaver-orders-web/-/merge_requests/2',
  commit_url: 'https://gitlab.com/beratersari0/yaver-orders-web/-/commit/bbb',
  commit_sha: 'bbb',
  commit_subject: 'web',
}

if (repositoryLabel(api) !== 'yaver-orders-api') throw new Error('api label')
const groups = groupDeliveries([api, web])
if (groups.length !== 2) throw new Error('expected two repos')
if (groups[0].repo !== 'yaver-orders-api' || groups[1].repo !== 'yaver-orders-web') {
  throw new Error('group order')
}
console.log('repo deliveries ok')
