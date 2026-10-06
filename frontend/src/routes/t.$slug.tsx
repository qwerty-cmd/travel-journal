import { createFileRoute, Outlet } from "@tanstack/react-router";
import { LegacyJoinPanel } from "../components/JoinRequest";
import { LegacyNotice } from "../components/LegacyNotice";
import { isTripNotFound, useTrip } from "../trip";
import { viewerRole } from "../tripV2";
import "./t.$slug.css";

// Design feature: trip shell for every /t/$slug screen (decision-log Entry 18).
// Loads the trip once for all child routes. Legacy routes are read-only
// (legacy-link.md); the rider's name now comes from the account. A cached trip renders even when the
// server can't be reached (Entry 19); only a NOT_FOUND envelope overrides it.
// Design format, first match wins:
//   - NOT_FOUND envelope → "Trip not found" (useTrip also clears the cached
//     trip and, if it was this slug, the remembered last slug).
//   - trip data (fetched or persisted) → header with trip name and
//     "Starts <startDate>", then LegacyNotice (variant by viewer.role: signed
//     out / member "Open trip"), LegacyJoinPanel, then the child route.
//   - any other error with no cached trip → "Can't reach the server" + Retry.
//   - otherwise → "Waking up the server…" (free-tier cold start).
//     LegacyJoinPanel: signed in with viewer.role none/pending (claim / pending).
// APIs called: GET /api/trips/{slug} via useTrip (src/trip.ts), seeded from the
// persisted TripOut; POST /api/v2/trips/claim from LegacyJoinPanel. Child routes read this same cache entry with
// refetchOnMount: false rather than fetching again.
export const Route = createFileRoute("/t/$slug")({
  component: TripShell,
});

function TripShell() {
  const { slug } = Route.useParams();
  const trip = useTrip(slug);

  if (isTripNotFound(trip.error)) return <p className="legacy__status">Trip not found</p>;

  if (trip.data) {
    return (
      <>
        <header className="legacy__header">
          <h1 className="legacy__name">{trip.data.name}</h1>
          <p className="legacy__meta">Starts {trip.data.startDate}</p>
        </header>
        <LegacyNotice slug={slug} tripId={trip.data.id} role={viewerRole(trip.data)} />
        {/* Signed-in non-member: one-tap claim of this rider link (a join request, never access). */}
        <LegacyJoinPanel slug={slug} tripId={trip.data.id} role={viewerRole(trip.data)} onChanged={() => void trip.refetch()} />
        <Outlet />
      </>
    );
  }

  if (trip.isError) {
    return (
      <div className="legacy__status">
        <p>Can't reach the server</p>
        <button type="button" onClick={() => trip.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  // Cold start: the free-tier container may take a while to wake.
  return <p className="legacy__status">Waking up the server…</p>;
}
