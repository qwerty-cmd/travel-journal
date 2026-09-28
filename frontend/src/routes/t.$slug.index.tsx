import { createFileRoute } from "@tanstack/react-router";
import { useGetMapApiTripsSlugMapGet } from "../api/gen/hooks/useGetMapApiTripsSlugMapGet";
import { TripMap } from "../components/TripMap";

// Trip home (spec Section 6, screen 2). The timeline lands here in
// t-frontend-timeline-feed.
export const Route = createFileRoute("/t/$slug/")({
  component: TripHome,
});

function TripHome() {
  const { slug } = Route.useParams();
  const map = useGetMapApiTripsSlugMapGet({ slug });

  return (
    <main style={{ padding: 16 }}>
      {map.isError ? <p>Map unavailable</p> : <TripMap collection={map.data} />}
    </main>
  );
}
