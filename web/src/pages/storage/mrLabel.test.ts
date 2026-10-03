import { storageMrLabel } from './mrLabel'

const api = 'https://gitlab.com/beratersari0/yaver-orders-api/-/merge_requests/7'
const web = 'https://gitlab.com/beratersari0/yaver-orders-web/-/merge_requests/7'
const plain = 'https://gitlab.example/group/repo/merge_requests/4'
const pr = 'https://tfs.example/tfs/Col/Proj/_git/Repo/pullrequest/12'

if (storageMrLabel(api) !== '!7') throw new Error(`api ${storageMrLabel(api)}`)
if (storageMrLabel(web) !== '!7') throw new Error(`web ${storageMrLabel(web)}`)
if (storageMrLabel(api) !== storageMrLabel(web)) {
  throw new Error('a multi-repo folder uses the same !N label as a single repo')
}
if (storageMrLabel(plain) !== '!4') throw new Error(`plain ${storageMrLabel(plain)}`)
if (storageMrLabel(pr) !== '!12') throw new Error(`pr ${storageMrLabel(pr)}`)
if (storageMrLabel(api).includes('yaver-orders-api') || storageMrLabel(api).includes(' !')) {
  throw new Error('the repository path is not part of the Storage label')
}
if (storageMrLabel('https://gitlab.com/group/repo') !== 'MR') {
  throw new Error('a url with no review id stays MR')
}

console.log('storage mr label ok')
