/** Chrome allows 6 HTTP/1.1 connections per host, and the live socket uses one.
 * Four GETs leave a connection for Sign out, Stop, and Save. A fifth GET
 * waits here, in JavaScript, instead of filling the browser queue.
 * Background GETs stop one slot earlier so the page on screen can start
 * while boot poll, settings, and the queue count are still running. */

export const MAX_CONCURRENT_GETS = 4
export const MAX_BACKGROUND_GETS = 3

export type GetSlot = 'page' | 'background'

let active = 0
let background = 0

type Waiter = {
  kind: GetSlot
  start: () => void
}

const waiters: Waiter[] = []

function abortError(): DOMException {
  return new DOMException('Aborted', 'AbortError')
}

function canStart(kind: GetSlot): boolean {
  if (active >= MAX_CONCURRENT_GETS) return false
  if (kind === 'background' && background >= MAX_BACKGROUND_GETS) return false
  return true
}

function take(kind: GetSlot) {
  active += 1
  if (kind === 'background') background += 1
}

function pump() {
  const pageIndex = waiters.findIndex((waiter) => waiter.kind === 'page' && canStart('page'))
  const index = pageIndex >= 0 ? pageIndex : waiters.findIndex((waiter) => canStart(waiter.kind))
  if (index < 0) return
  const [waiter] = waiters.splice(index, 1)
  waiter.start()
}

export function acquireGetSlot(signal?: AbortSignal, kind: GetSlot = 'page'): Promise<void> {
  if (signal?.aborted) return Promise.reject(abortError())
  if (canStart(kind)) {
    take(kind)
    return Promise.resolve()
  }
  return new Promise((resolve, reject) => {
    let settled = false
    const waiter: Waiter = {
      kind,
      start: () => {
        if (settled) return
        settled = true
        signal?.removeEventListener('abort', onAbort)
        take(kind)
        resolve()
      },
    }
    const onAbort = () => {
      if (settled) return
      settled = true
      const index = waiters.indexOf(waiter)
      if (index >= 0) waiters.splice(index, 1)
      reject(abortError())
    }
    if (signal) signal.addEventListener('abort', onAbort, { once: true })
    waiters.push(waiter)
  })
}

export function releaseGetSlot(kind: GetSlot = 'page'): void {
  active = Math.max(0, active - 1)
  if (kind === 'background') background = Math.max(0, background - 1)
  pump()
}

export function resetGetSlotsForTests(): void {
  active = 0
  background = 0
  waiters.length = 0
}
