/** Storage label for one review: ``!N``, same text a single-repo folder already used. */
export function storageMrLabel(url: string): string {
  const gl = /\/merge_requests\/(\d+)/i.exec(url)
  if (gl) return `!${gl[1]}`
  const az = /\/pullrequest\/(\d+)/i.exec(url)
  if (az) return `!${az[1]}`
  return 'MR'
}
