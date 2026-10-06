import { createFileRoute, Navigate } from "@tanstack/react-router";
import { useGetTripApiTripsSlugGet } from "../api/gen/hooks/useGetTripApiTripsSlugGet";
import { isMember } from "../tripV2";

// Design feature: retired legacy add-stop entry (legacy-link.md): legacy routes are
// read-only, so an old bookmark of /t/$slug/add no longer offers a form.
// Design format: no UI of its own. A member is redirected to the v2 add screen
// (/trips/$tripId/add); anyone else to /t/$slug, whose notice explains the change.
// Already-queued legacy stops still drain against the legacy paths (src/offline/queue.ts).
// APIs called: none; GET /api/trips/{slug} is read from the shell's cache.
export const Route = createFileRoute("/t/$slug/add")({
  component: LegacyAdd,
});

function LegacyAdd() {
  const { slug } = Route.useParams();
  const trip = useGetTripApiTripsSlugGet({ slug }, { query: { refetchOnMount: false } });
  if (trip.data && isMember(trip.data)) return <Navigate to="/trips/$tripId/add" params={{ tripId: trip.data.id }} replace />;
  return <Navigate to="/t/$slug" params={{ slug }} replace />;
}
