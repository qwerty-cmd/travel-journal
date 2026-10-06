/**
 * Session state and the client-side account rules shared by /signin, /signup
 * and /account (decision-log Entry 29; docs/api-contract.md "Sessions" and
 * "Notes per endpoint (v2)").
 *
 * - Who is signed in comes from the generated GET /api/v2/auth/me query. On a
 *   200 `{id, displayName}` is cached in localStorage `btj.me` for offline cold
 *   opens; on a 401 (no session) the cache is cleared.
 * - Sign-in and sign-out each post one message on the BroadcastChannel `auth`
 *   (`{ type: 'signin' | 'signout' }`) so other tabs and the offline queue can react.
 * - Client checks mirror the contract so most errors are caught before a round
 *   trip; the server stays the authority (DESIGN.md §8.1).
 *
 * APIs called: GET /api/v2/auth/me (through the generated hook).
 */
import { useEffect, useRef, useState } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { ApiError } from './api/client'
import { getMeApiV2AuthMeGetQueryKey, useGetMeApiV2AuthMeGet } from './api/gen/hooks/useGetMeApiV2AuthMeGet'
import type { MeOut } from './api/gen/types/MeOut'
import { clearCachedMe, setCachedMe } from './localStore'

export const AUTH_CHANNEL = 'auth'
export type AuthMessage = { type: 'signin' | 'signout' }

export function broadcastAuth(type: AuthMessage['type']): void {
  if (typeof BroadcastChannel !== 'function') return
  const channel = new BroadcastChannel(AUTH_CHANNEL)
  channel.postMessage({ type } satisfies AuthMessage)
  channel.close()
}

/** The signed-in account, or a 401 error when nobody is. Never retries an answered request. */
export function useMe() {
  const me = useGetMeApiV2AuthMeGet({
    query: { retry: (count, error) => error.status === undefined && count < 3 },
  })
  useEffect(() => {
    if (me.data) setCachedMe(me.data)
    else if (me.error?.status === 401) clearCachedMe()
  }, [me.data, me.error])
  return me
}

/** After signin, signup or a password change: the response's MeOut becomes the session state. */
export function markSignedIn(queryClient: QueryClient, me: MeOut, broadcast = true): void {
  queryClient.setQueryData(getMeApiV2AuthMeGetQueryKey(), me)
  setCachedMe(me)
  if (broadcast) broadcastAuth('signin')
}

/** After signout or signout-all. The offline queue is deliberately left alone. */
export function markSignedOut(queryClient: QueryClient): void {
  clearCachedMe()
  queryClient.removeQueries({ queryKey: getMeApiV2AuthMeGetQueryKey() })
  broadcastAuth('signout')
}

/** `?next=` accepts only a same-origin path: "/x" yes, "//evil" and "https://…" no. */
export function safeNext(next: unknown): string {
  return typeof next === 'string' && next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/\\') ? next : '/'
}

// --- Client-side rules (contract "Notes per endpoint (v2)": Signup) --------

export const USERNAME_RE = /^[a-z0-9][a-z0-9_.-]{2,31}$/

/** Length in code points, as the server counts (a surrogate pair is one). */
export const codePoints = (s: string) => [...s].length

export function displayNameError(value: string): string | undefined {
  const trimmed = value.trim()
  if (!trimmed) return 'Enter a display name.'
  if (codePoints(trimmed) > 40) return 'Use 40 characters or fewer.'
  return undefined
}

export function usernameError(value: string): string | undefined {
  if (!value.trim()) return 'Enter a username.'
  if (!USERNAME_RE.test(value.toLowerCase())) {
    return 'Use 3 to 32 letters, numbers, dots, dashes or underscores, starting with a letter or number.'
  }
  return undefined
}

/** New passwords: 15–128 code points after NFKC, as the server normalises before counting. */
export function newPasswordError(value: string): string | undefined {
  const n = codePoints(value.normalize('NFKC'))
  if (n < 15) return 'Use at least 15 characters.'
  if (n > 128) return 'Use 128 characters or fewer.'
  return undefined
}

// --- Errors and rate limits -------------------------------------------------

export const isOffline = (e: unknown) => e instanceof ApiError && e.status === undefined
export const isRateLimited = (e: unknown) => e instanceof ApiError && e.status === 429

/** The server's envelope message when there is one; otherwise a generic line. */
export function errorText(e: unknown, offlineText: string): string {
  if (e instanceof ApiError) {
    if (e.envelope) return e.envelope.error.message
    if (e.status === undefined) return offlineText
  }
  return 'Something went wrong. Try again.'
}

/** DESIGN.md §5.6 rounding: seconds under a minute, whole minutes under 2 h, else hours (rounded up). */
export function formatRetry(seconds: number): string {
  const plural = (n: number, unit: string) => `Try again in ${n} ${unit}${n === 1 ? '' : 's'}.`
  if (seconds < 60) return plural(Math.max(1, Math.ceil(seconds)), 'second')
  if (seconds < 7200) return plural(Math.ceil(seconds / 60), 'minute')
  return plural(Math.ceil(seconds / 3600), 'hour')
}

/** Seconds left on a 429's Retry-After; ticks once a second and stops at 0. */
export function useRetryCountdown() {
  const [until, setUntil] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (until === null) return
    const id = setInterval(() => {
      const t = Date.now()
      setNow(t)
      if (t >= until) setUntil(null)
    }, 1000)
    return () => clearInterval(id)
  }, [until])
  const remaining = until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000))
  return {
    remaining,
    /** Starts the countdown when `e` is a 429 with a Retry-After. */
    startFrom(e: unknown) {
      if (e instanceof ApiError && e.status === 429 && e.retryAfter !== undefined) {
        const t = Date.now()
        setNow(t)
        setUntil(t + e.retryAfter * 1000)
      }
    },
  }
}

/** True once `active` has lasted 3 s: the cold-start "Waking up the server…" hint (DESIGN.md §5.11). */
export function useSlow(active: boolean): boolean {
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    setSlow(false)
    if (!active) return
    const id = setTimeout(() => setSlow(true), 3000)
    return () => clearTimeout(id)
  }, [active])
  return slow
}

/**
 * One request at a time for a form: a second submit while the first is in
 * flight is dropped. A ref, not the mutation's isPending, because two taps can
 * land before React re-renders the disabled button.
 */
export function useSingleFlight() {
  const busy = useRef(false)
  return {
    run(start: (done: () => void) => void) {
      if (busy.current) return
      busy.current = true
      start(() => {
        busy.current = false
      })
    },
  }
}

/** iOS Safari outside the installed app: show the DESIGN.md §10.3 line (Entry 18). */
export function isIosBrowserTab(): boolean {
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent)
  const standalone = (navigator as { standalone?: boolean }).standalone === true
  return ios && !standalone
}

export const IOS_LINE =
  'On iPhone, add this app to your Home Screen and sign in inside the installed app. The installed app keeps its own sign-in and its own unsent stops, separate from Safari.'
