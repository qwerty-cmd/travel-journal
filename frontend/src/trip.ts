/**
 * Trip-link parsing and the one hook every `/t/$slug` screen reads the trip
 * through. Lives outside `routes/` because route files are code-split and must
 * not export helpers.
 */
import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError } from './api/client'
import { useGetTripApiTripsSlugGet } from './api/gen/hooks/useGetTripApiTripsSlugGet'
import { clearLastSlug, clearTrip, getLastSlug, loadTrip, saveTrip, setLastSlug } from './localStore'

/**
 * Accepts what a rider pastes on the `/` screen (decision-log Entry 18): a full
 * URL whose path is `/t/<slug>` (anything after the slug ignored), or a bare
 * slug with no `/`. Returns the slug, or null for anything else.
 */
export function parseTripLink(input: string): string | null {
  const text = input.trim()
  if (!text) return null
  if (!text.includes('/')) return text
  try {
    return new URL(text).pathname.match(/^\/t\/([^/]+)/)?.[1] ?? null
  } catch {
    return null
  }
}

/** True only for a server-sent NOT_FOUND envelope; a bare 404 is "unreachable". */
export const isTripNotFound = (error: unknown) =>
  error instanceof ApiError && error.envelope?.error.code === 'NOT_FOUND'

/**
 * `GET /api/trips/{slug}` with the persisted TripOut as `initialData`
 * (decision-log Entry 19), so a cold open offline still has a trip to render.
 * An enveloped error is the server's answer and is not retried; anything else
 * (network, 502 page, non-ApiError) uses the QueryClient's retry default.
 */
export function useTrip(slug: string) {
  const defaultRetry = useQueryClient().getDefaultOptions().queries?.retry ?? 3

  const query = useGetTripApiTripsSlugGet(
    { slug },
    {
      query: {
        initialData: () => loadTrip(slug),
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.envelope) return false
          if (typeof defaultRetry === 'function') return defaultRetry(failureCount, error)
          if (typeof defaultRetry === 'boolean') return defaultRetry
          return failureCount < defaultRetry
        },
      },
    },
  )

  // `isFetched` is false while the data is only the cached initialData, so the
  // cache is written back only after a real response.
  const fetchedTrip = query.isSuccess && query.isFetched ? query.data : undefined
  useEffect(() => {
    if (!fetchedTrip) return
    saveTrip(slug, fetchedTrip)
    setLastSlug(slug)
  }, [slug, fetchedTrip, query.dataUpdatedAt])

  const notFound = isTripNotFound(query.error)
  useEffect(() => {
    if (!notFound) return
    clearTrip(slug)
    if (getLastSlug() === slug) clearLastSlug()
  }, [slug, notFound])

  return query
}
