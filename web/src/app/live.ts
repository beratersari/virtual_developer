import { createContext, useContext } from 'react'
import type { Meta, PollPayload, SettingsPayload } from '../api/types'

export type LiveValue = {
  connected: boolean
  meta: Meta | null
  poll: PollPayload | null
  settings: SettingsPayload | null
  generation: number
  pollCountdown: number | null
  error: string | null
}

export const LiveContext = createContext<LiveValue | null>(null)

export function useLive(): LiveValue {
  const ctx = useContext(LiveContext)
  if (!ctx) throw new Error('useLive must be used within LiveProvider')
  return ctx
}
