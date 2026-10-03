import { createContext, useContext, useLayoutEffect, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import {
  applyDocumentTitle,
  pageNameFromLocation,
  recordTitle,
  shownPageName,
  visiblePageName,
} from '../util/pageTitle'

type TitleOverride = { path: string; name: string }

const PageTitleOverride = createContext<(next: TitleOverride) => void>(() => {})
const ShownPageTitle = createContext('Yaver')

export function usePageTitle(name: string) {
  const setOverride = useContext(PageTitleOverride)
  const { pathname } = useLocation()
  useLayoutEffect(() => {
    setOverride({ path: pathname, name })
  }, [setOverride, pathname, name])
}

/** Same name the browser tab shows for this address, without the Yaver prefix. */
export function useShownPageTitle(): string {
  return useContext(ShownPageTitle)
}

/** Issue, job, or workspace name. Empty on list pages and before the record loads. */
export function useRecordTitle(): string | null {
  const visible = useShownPageTitle()
  const { pathname, search } = useLocation()
  return recordTitle(visible, pageNameFromLocation(pathname, search))
}

/** Sets document.title from the address, unless the open record supplies its own name. */
export function PageTitleRoot({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation()
  const [override, setOverride] = useState<TitleOverride | null>(null)
  const fallback = pageNameFromLocation(pathname, search)
  const shown = shownPageName(fallback, override, pathname)
  const visible = visiblePageName(shown)

  useLayoutEffect(() => {
    applyDocumentTitle(shown)
  }, [shown])

  return (
    <PageTitleOverride.Provider value={setOverride}>
      <ShownPageTitle.Provider value={visible}>{children}</ShownPageTitle.Provider>
    </PageTitleOverride.Provider>
  )
}
