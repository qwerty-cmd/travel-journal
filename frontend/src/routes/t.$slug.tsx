import { useState } from "react";
import { createFileRoute, Outlet } from "@tanstack/react-router";
import { DisplayNamePrompt } from "../components/DisplayNamePrompt";
import { getDisplayName } from "../localStore";
import { isTripNotFound, useTrip } from "../trip";

// Design feature: trip shell for every /t/$slug screen (decision-log Entry 18).
// Loads the trip once for all child routes, and asks a rider for a display
// name before any child screen is shown. A cached trip renders even when the
// server can't be reached (Entry 19); only a NOT_FOUND envelope overrides it.
// Design format, first match wins:
//   - NOT_FOUND envelope → "Trip not found" (useTrip also clears the cached
//     trip and, if it was this slug, the remembered last slug).
//   - trip data (fetched or persisted) → header with trip name and
//     "Starts <startDate>", then either DisplayNamePrompt (access === "rider"
//     and no name saved on this device) or the child route. Rider vs viewer
//     comes from the server's `access`, never from device storage; viewers are
//     never asked for a name.
//   - any other error with no cached trip → "Can't reach the server" + Retry.
//   - otherwise → "Waking up the server…" (free-tier cold start).
// APIs called: GET /api/trips/{slug} via useTrip (src/trip.ts), seeded from the
// persisted TripOut. Child routes read this same cache entry with
// refetchOnMount: false rather than fetching again.
export const Route = createFileRoute("/t/$slug")({
  component: TripShell,
});

function TripShell() {
  const { slug } = Route.useParams();
  const trip = useTrip(slug);
  const [hasName, setHasName] = useState(() => getDisplayName() !== null);

  if (isTripNotFound(trip.error)) return <p style={{ padding: 16 }}>Trip not found</p>;

  if (trip.data) {
    return (
      <>
        <header style={{ padding: 16 }}>
          <h1 style={{ margin: 0 }}>{trip.data.name}</h1>
          <p style={{ margin: 0 }}>Starts {trip.data.startDate}</p>
        </header>
        {/* Rider-only (Entry 18): decided by the server's access, never device storage. */}
        {trip.data.access === "rider" && !hasName ? (
          <DisplayNamePrompt onSaved={() => setHasName(true)} />
        ) : (
          <Outlet />
        )}
      </>
    );
  }

  if (trip.isError) {
    return (
      <div style={{ padding: 16 }}>
        <p>Can't reach the server</p>
        <button type="button" onClick={() => trip.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  // Cold start: the free-tier container may take a while to wake.
  return <p style={{ padding: 16 }}>Waking up the server…</p>;
}
