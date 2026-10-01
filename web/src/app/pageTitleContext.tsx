import { createContext, useContext, useLayoutEffect, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { applyDocumentTitle, pageNameFromLocation } from '../util/pageTitle'

type TitleOverride = { path: string; name: string }

const PageTitleOverride = createContext<(next: TitleOverride) => void>(() => {})

export function usePageTitle(name: string) {
  const setOverride = useContext(PageTitleOverride)
  const { pathname } = useLocation()
  useLayoutEffect(() => {
    setOverride({ path: pathname, name })
  }, [setOverride, pathname, name])
}

/** Sets document.title from the address, unless the open record supplies its own name. */
export function PageTitleRoot({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation()
  const [override, setOverride] = useState<TitleOverride | null>(null)
  const fallback = pageNameFromLocation(pathname, search)
  const shown = override && override.path === pathname ? override.name : fallback

  useLayoutEffect(() => {
    applyDocumentTitle(shown)
  }, [shown])

  return <PageTitleOverride.Provider value={setOverride}>{children}</PageTitleOverride.Provider>
}
