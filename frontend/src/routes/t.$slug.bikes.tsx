import { createFileRoute, Link } from "@tanstack/react-router";
import { useGetTripApiTripsSlugGet } from "../api/gen/hooks/useGetTripApiTripsSlugGet";
import "./trips.$tripId.bikes.css";
import "./t.$slug.css";

// Design feature: Bikes page (spec Section 6), read-only on legacy routes
// (legacy-link.md): no Add bike / Edit, whatever the legacy `access`. Bike
// editing lives on the v2 trip.
// Design format: back link, then one card per bike (rider name, make, model,
// year, specs with line breaks kept), sorted by rider name (TripOut.bikes
// order is not contract). Empty specs render no specs block; no bikes → "No bikes yet".
// APIs called: GET /api/trips/{slug} (TripOut.bikes) from the cache the trip
// shell (useTrip) already loaded, so the list works from a persisted trip offline.
export const Route = createFileRoute("/t/$slug/bikes")({
  component: Bikes,
});

function Bikes() {
  const { slug } = Route.useParams();
  // Reads the trip the shell (useTrip) already loaded; no second fetch on mount.
  const trip = useGetTripApiTripsSlugGet({ slug }, { query: { refetchOnMount: false } });
  const bikes = [...(trip.data?.bikes ?? [])].sort((a, b) => a.riderName.localeCompare(b.riderName));

  return (
    <main className="bikes">
      <Link to="/t/$slug" params={{ slug }} className="btn btn--tertiary btn--md">
        Back to trip
      </Link>
      <h2 className="legacy__h2">Bikes</h2>
      {bikes.length === 0 ? (
        <p>No bikes yet</p>
      ) : (
        <ul className="bikes__list">
          {bikes.map((bike) => (
            <li key={bike.id} className="bikes__card">
              <h3 className="bikes__rider">{bike.riderName}</h3>
              <p>
                {bike.make} {bike.model} ({bike.year})
              </p>
              {bike.specs !== "" && <p className="bikes__specs">{bike.specs}</p>}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
