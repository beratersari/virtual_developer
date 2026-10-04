import { useSyncExternalStore } from 'react'

/** Saved on this browser. Missing or unknown values stay on the dark console. */
export const THEME_STORAGE_KEY = 'yaver.theme'

export type Theme = 'dark' | 'light'

export function parseTheme(raw: string | null | undefined): Theme {
  return raw === 'light' ? 'light' : 'dark'
}

export function themeFromStorage(read: (key: string) => string | null): Theme {
  try {
    return parseTheme(read(THEME_STORAGE_KEY))
  } catch {
    return 'dark'
  }
}

let current: Theme = 'dark'
const listeners = new Set<() => void>()

function commit(next: Theme, persist: boolean) {
  current = next
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = next
    document.documentElement.style.colorScheme = next
  }
  if (persist && typeof localStorage !== 'undefined') {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      /* private mode or a full store still applies for this tab */
    }
  }
  for (const listener of listeners) listener()
}

/** Apply the saved theme before the first paint of the React tree. */
export function installTheme() {
  if (typeof localStorage === 'undefined') return
  commit(themeFromStorage((key) => localStorage.getItem(key)), false)
}

export function setTheme(next: Theme) {
  commit(next, true)
}

export function useTheme(): Theme {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
    () => 'dark',
  )
}
