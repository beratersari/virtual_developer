/**
 * Run: npx tsx src/api/getSlots.test.ts
 *
 * Chrome allows 6 connections per host. Dashboard GETs stay at 4 so a
 * WebSocket and a button (Sign out, Stop, Save) still have a connection.
 * Background GETs (boot poll, settings, queue) stop at 3 so the open page
 * can still start.
 */
import {
  MAX_BACKGROUND_GETS,
  MAX_CONCURRENT_GETS,
  acquireGetSlot,
  releaseGetSlot,
  resetGetSlotsForTests,
} from './getSlots'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

function read(box: { n: number }): number {
  return box.n
}

async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

async function main() {
  assert(MAX_CONCURRENT_GETS === 4, 'four GETs leave room for the socket and a button')
  assert(MAX_BACKGROUND_GETS === 3, 'one slot stays free for the page on screen')
  resetGetSlotsForTests()

  const held: Promise<void>[] = []
  for (let i = 0; i < MAX_CONCURRENT_GETS; i += 1) held.push(acquireGetSlot())
  await Promise.all(held)

  const fifth = { n: 0 }
  const waiting = acquireGetSlot().then(() => {
    fifth.n += 1
  })
  await flush()
  assert(read(fifth) === 0, 'a fifth GET waits instead of taking the last browser sockets')

  const cancelled = new AbortController()
  const dropped = { n: 0 }
  const abandoned = acquireGetSlot(cancelled.signal).then(
    () => {
      dropped.n += 1
    },
    () => {
      dropped.n += 1
    },
  )
  cancelled.abort()
  await abandoned
  assert(read(dropped) === 1, 'an aborted waiter finishes')

  releaseGetSlot()
  await waiting
  await flush()
  assert(read(fifth) === 1, 'the waiting GET starts when a slot frees')
  assert(read(dropped) === 1, 'the aborted waiter does not take that slot')

  releaseGetSlot()
  resetGetSlotsForTests()

  const background: Promise<void>[] = []
  for (let i = 0; i < MAX_BACKGROUND_GETS; i += 1) {
    background.push(acquireGetSlot(undefined, 'background'))
  }
  await Promise.all(background)

  const extraBackground = { n: 0 }
  const blocked = acquireGetSlot(undefined, 'background').then(() => {
    extraBackground.n += 1
  })
  const page = { n: 0 }
  const visible = acquireGetSlot(undefined, 'page').then(() => {
    page.n += 1
  })
  await flush()
  assert(read(extraBackground) === 0, 'a fourth background GET waits')
  assert(read(page) === 1, 'the open page still starts while background GETs are full')
  await visible

  const queuedPage = { n: 0 }
  const queued = acquireGetSlot(undefined, 'page').then(() => {
    queuedPage.n += 1
  })
  await flush()
  assert(read(queuedPage) === 0, 'the fifth GET, even a page, waits at the cap')

  releaseGetSlot('page')
  await queued
  await flush()
  assert(read(queuedPage) === 1, 'a freed slot goes to the waiting page')
  assert(read(extraBackground) === 0, 'the waiting page starts ahead of a background GET')

  releaseGetSlot('background')
  await blocked
  await flush()
  assert(read(extraBackground) === 1, 'a background GET starts once a background slot is free')

  resetGetSlotsForTests()
  console.log('getSlots.test.ts: ok')
}

main().catch((err: unknown) => {
  console.error(err)
  throw err
})
