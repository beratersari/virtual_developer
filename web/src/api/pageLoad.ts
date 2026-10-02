import { useEffect, useRef } from 'react'

/** One in-flight GET for a page.
 *
 * A live tick must not abort that GET (aborting a slow Analytics request on
 * every tick left the spinner up). It also must not start a second GET
 * (stacked GETs fill Chrome's 6 connections, and the next page stays on
 * Loading until a refresh aborts them). Changing the query aborts the
 * previous GET so the socket is free immediately. */

export type PageLoad = {
  query(key: string): AbortSignal | null
  tick(key: string): AbortSignal | null
  settle(signal: AbortSignal): boolean
  dispose(): void
}

export function createPageLoad(): PageLoad {
  let ctrl: AbortController | null = null
  let activeKey = ''
  let again = false

  const start = (): AbortSignal => {
    const ac = new AbortController()
    ctrl = ac
    return ac.signal
  }

  return {
    query(key: string) {
      if (ctrl && !ctrl.signal.aborted && activeKey === key) return null
      again = false
      ctrl?.abort()
      activeKey = key
      return start()
    },
    tick(key: string) {
      if (ctrl && !ctrl.signal.aborted) {
        again = true
        return null
      }
      activeKey = key
      again = false
      return start()
    },
    settle(signal: AbortSignal) {
      if (!ctrl || ctrl.signal !== signal) return false
      const follow = again && !signal.aborted
      ctrl = null
      again = false
      return follow
    },
    dispose() {
      again = false
      ctrl?.abort()
      ctrl = null
    },
  }
}

/** Mount and filter changes are not a new live tick. */
export function noteLiveGeneration(
  seen: { current: number | null },
  generation: number,
): boolean {
  if (seen.current === null) {
    seen.current = generation
    return false
  }
  if (seen.current === generation) return false
  seen.current = generation
  return true
}

export function usePageLoad(): PageLoad {
  const ref = useRef<PageLoad | null>(null)
  if (!ref.current) ref.current = createPageLoad()
  useEffect(() => () => ref.current?.dispose(), [])
  return ref.current
}
