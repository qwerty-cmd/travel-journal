import { createFileRoute, Link } from "@tanstack/react-router";
import { useGetMapApiTripsSlugMapGet } from "../api/gen/hooks/useGetMapApiTripsSlugMapGet";
import { useListStopsApiTripsSlugStopsGet } from "../api/gen/hooks/useListStopsApiTripsSlugStopsGet";
import { Timeline } from "../components/Timeline";
import { TripMap } from "../components/TripMap";
import "./t.$slug.css";

// Design feature: trip home (spec Section 6, screen 2). Where the journey is
// seen at a glance: map of stops and trail, then the chronological timeline.
// Read-only for everyone (legacy-link.md): no Add stop on legacy routes.
// Design format: "Bikes" link, then TripMap ("Map unavailable" if the map
// request fails; Australia while loading), then the Timeline ("Loading stops…"
// while pending; the envelope message or "Couldn't load stops" on error).
// Stops queued offline but not yet sent do not appear here; the root
// QueueNotice shows them.
// APIs called: GET /api/trips/{slug}/map (useGetMapApiTripsSlugMapGet), GET
// /api/trips/{slug}/stops (useListStopsApiTripsSlugStopsGet). The
// queue drain invalidates the map and stops queries after each sent stop.
export const Route = createFileRoute("/t/$slug/")({
  component: TripHome,
});

function TripHome() {
  const { slug } = Route.useParams();
  const map = useGetMapApiTripsSlugMapGet({ slug });
  const stops = useListStopsApiTripsSlugStopsGet({ slug });

  return (
    <main className="legacy__panel">
      <Link to="/t/$slug/bikes" params={{ slug }}>
        Bikes
      </Link>
      {map.isError ? <p>Map unavailable</p> : <TripMap collection={map.data} />}
      {stops.isPending ? (
        <p>Loading stops…</p>
      ) : stops.isError ? (
        <p>{stops.error.envelope?.error.message ?? "Couldn't load stops"}</p>
      ) : (
        <Timeline stops={stops.data} />
      )}
    </main>
  );
}
