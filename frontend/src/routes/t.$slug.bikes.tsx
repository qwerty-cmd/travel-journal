import { createFileRoute, Link } from "@tanstack/react-router";
import { useGetTripApiTripsSlugGet } from "../api/gen/hooks/useGetTripApiTripsSlugGet";

// Design feature: Bikes page (spec Section 6) — every bike on the trip,
// read-only, same list for rider and viewer.
// Design format: back link, then one card per bike (rider name, make, model,
// year, specs with line breaks kept), sorted by rider name — TripOut.bikes
// order is not contract ("match bikes by id"), so the client sorts. Empty
// specs ("") render no specs block; no bikes → "No bikes yet".
// APIs called: none new — reads GET /api/trips/{slug} (TripOut.bikes) from the
// cache the trip shell (useTrip) already loaded, so it works from a persisted
// trip while offline.
export const Route = createFileRoute("/t/$slug/bikes")({
  component: Bikes,
});

function Bikes() {
  const { slug } = Route.useParams();
  // Reads the trip the shell (useTrip) already loaded; no second fetch on mount.
  const trip = useGetTripApiTripsSlugGet({ slug }, { query: { refetchOnMount: false } });
  const bikes = [...(trip.data?.bikes ?? [])].sort((a, b) => a.riderName.localeCompare(b.riderName));

  return (
    <main style={{ padding: 16 }}>
      <Link to="/t/$slug" params={{ slug }}>
        Back to trip
      </Link>
      <h2>Bikes</h2>
      {bikes.length === 0 ? (
        <p>No bikes yet</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0 }}>
          {bikes.map((bike) => (
            <li key={bike.id} style={{ marginBottom: 16 }}>
              <h3 style={{ margin: 0 }}>{bike.riderName}</h3>
              <p style={{ margin: 0 }}>
                {bike.make} {bike.model} ({bike.year})
              </p>
              {bike.specs !== "" && <p style={{ whiteSpace: "pre-wrap" }}>{bike.specs}</p>}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
