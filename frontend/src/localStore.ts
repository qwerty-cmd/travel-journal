/**
 * Everything this app keeps in localStorage, behind one module so the keys live
 * in one place. Every access is wrapped: storage that throws (Safari private
 * mode, quota, disabled) or holds corrupt JSON reads as "absent", never as an
 * exception the UI has to handle.
 *
 * APIs called: none. Stores the last fetched TripOut per slug (written by
 * useTrip after a real GET /api/trips/{slug} response), and `btj.me`: the
 * signed-in account's `{id, displayName}` from GET /api/v2/auth/me, for offline
 * cold opens. Never the username (private), the recovery code or any password.
 */
import type { TripOut } from './api/gen/types/TripOut'

export const LAST_SLUG_KEY = 'btj.lastSlug'
export const DISPLAY_NAME_KEY = 'btj.displayName'
export const tripKey = (slug: string) => `btj.trip.${slug}`

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Storage unavailable or full: the app still works, it just won't remember.
  }
}

function remove(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    // Nothing to clean up if storage is unavailable.
  }
}

export const getLastSlug = () => read(LAST_SLUG_KEY) || null
export const setLastSlug = (slug: string) => write(LAST_SLUG_KEY, slug)
export const clearLastSlug = () => remove(LAST_SLUG_KEY)

/** The TripOut last fetched for `slug` (decision-log Entry 19), or undefined. */
export function loadTrip(slug: string): TripOut | undefined {
  const raw = read(tripKey(slug))
  if (raw === null) return undefined
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as TripOut) : undefined
  } catch {
    return undefined
  }
}

export const saveTrip = (slug: string, trip: TripOut) => write(tripKey(slug), JSON.stringify(trip))
export const clearTrip = (slug: string) => remove(tripKey(slug))

export const getDisplayName = () => read(DISPLAY_NAME_KEY) || null
export const setDisplayName = (name: string) => write(DISPLAY_NAME_KEY, name)

export const ME_KEY = 'btj.me'

/** The cached signed-in account: id and public display name only (no username). */
export type CachedMe = { id: string; displayName: string }

export function getCachedMe(): CachedMe | undefined {
  const raw = read(ME_KEY)
  if (raw === null) return undefined
  try {
    const parsed = JSON.parse(raw) as Partial<CachedMe> | null
    return typeof parsed?.id === 'string' && typeof parsed.displayName === 'string'
      ? { id: parsed.id, displayName: parsed.displayName }
      : undefined
  } catch {
    return undefined
  }
}

/** Picks the two fields explicitly, so a MeOut passed in never leaks its username. */
export const setCachedMe = ({ id, displayName }: CachedMe) => write(ME_KEY, JSON.stringify({ id, displayName }))
export const clearCachedMe = () => remove(ME_KEY)
