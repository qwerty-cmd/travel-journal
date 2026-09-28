import { createFileRoute } from "@tanstack/react-router";
import { useGetMapApiTripsSlugMapGet } from "../api/gen/hooks/useGetMapApiTripsSlugMapGet";
import { useListStopsApiTripsSlugStopsGet } from "../api/gen/hooks/useListStopsApiTripsSlugStopsGet";
import { Timeline } from "../components/Timeline";
import { TripMap } from "../components/TripMap";

// Trip home (spec Section 6, screen 2): map, then the chronological timeline.
export const Route = createFileRoute("/t/$slug/")({
  component: TripHome,
});

function TripHome() {
  const { slug } = Route.useParams();
  const map = useGetMapApiTripsSlugMapGet({ slug });
  const stops = useListStopsApiTripsSlugStopsGet({ slug });

  return (
    <main style={{ padding: 16 }}>
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
