/**
 * The one hook every `/trips/$tripId` screen reads the trip through, plus the
 * role helpers that decide what those screens render (decision-log Entry 29:
 * what renders comes from `TripOut.viewer.role`, never from device storage).
 * Lives outside `routes/` because route files are code-split and must not
 * export helpers.
 *
 * APIs called: GET /api/v2/trips/{tripId} (useTripV2).
 */
import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError } from './api/client'
import { useGetTripApiV2TripsTripIdGet } from './api/gen/hooks/useGetTripApiV2TripsTripIdGet'
import type { TripOut } from './api/gen/types/TripOut'
import type { ViewerRole } from './api/gen/types/ViewerRole'
import { clearTripById, loadTripById, saveTripById } from './localStore'

/**
 * "Trip not found" for a NOT_FOUND envelope (unknown and private-to-a-non-member
 * are byte-identical), and for a VALIDATION_ERROR on the read: an id that isn't
 * a UUID can't name a trip. Anything else is "unreachable".
 */
export const isTripV2NotFound = (error: unknown) =>
  error instanceof ApiError &&
  (error.envelope?.error.code === 'NOT_FOUND' || error.envelope?.error.code === 'VALIDATION_ERROR')

/**
 * The caller's role, or undefined when the trip record has no `viewer` (a
 * persisted TripOut from before the extension). Undefined grants nothing; the
 * query refetches on mount and the server's answer replaces it.
 */
export function viewerRole(trip: TripOut): ViewerRole | undefined {
  return (trip as Partial<TripOut>).viewer?.role
}

/**
 * Rider or leader: sees stops live. Pre-extension record: falls back to the
 * legacy `access`, which only ever chooses read-only copy (the delay note).
 * Write UI (later tasks) must gate on `viewerRole` alone, never on this.
 */
export function isMember(trip: TripOut): boolean {
  const role = viewerRole(trip)
  if (role === undefined) return trip.access === 'rider'
  return role === 'rider' || role === 'leader'
}

/**
 * DESIGN.md §9 viewer caption: non-members of a public trip with a delay.
 * Null when nothing should show (member, private trip, delay 0, or a record
 * without `visibility`).
 */
export function delayCaption(trip: TripOut): string | null {
  const visibility = (trip as Partial<TripOut>).visibility
  const hours = trip.publicDelayHours
  if (visibility !== 'public' || isMember(trip) || !(hours > 0)) return null
  return `Stops appear here ${hours} ${hours === 1 ? 'hour' : 'hours'} after they're added.`
}

/**
 * `GET /api/v2/trips/{tripId}` with the persisted TripOut for that id as
 * `initialData` (decision-log Entry 19), so a cold open offline still renders.
 * The record is written back only after a real response and cleared on "Trip
 * not found". An enveloped error is the server's answer and is not retried.
 */
export function useTripV2(tripId: string) {
  const defaultRetry = useQueryClient().getDefaultOptions().queries?.retry ?? 3

  const query = useGetTripApiV2TripsTripIdGet(
    { tripId },
    {
      query: {
        initialData: () => loadTripById(tripId),
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.envelope) return false
          if (typeof defaultRetry === 'function') return defaultRetry(failureCount, error)
          if (typeof defaultRetry === 'boolean') return defaultRetry
          return failureCount < defaultRetry
        },
      },
    },
  )

  // `isFetched` is false while the data is only the persisted initialData.
  const fetchedTrip = query.isSuccess && query.isFetched ? query.data : undefined
  useEffect(() => {
    if (fetchedTrip) saveTripById(tripId, fetchedTrip)
  }, [tripId, fetchedTrip, query.dataUpdatedAt])

  const notFound = isTripV2NotFound(query.error)
  useEffect(() => {
    if (notFound) clearTripById(tripId)
  }, [tripId, notFound])

  return query
}
