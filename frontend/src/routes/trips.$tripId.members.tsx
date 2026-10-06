import { createFileRoute, Link } from "@tanstack/react-router";
import { useGetTripApiV2TripsTripIdGet } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import { MembersView } from "../components/MembersView";
import { LeaderReview, type ReviewView } from "../components/LeaderReview";
import { StatusNotice } from "../components/StatusNotice";
import { viewerRole } from "../tripV2";

// Design feature: the leader area at /trips/$tripId/members?view=requests|members
// (docs/design/screens/leader-review.md; DESIGN.md §13 ruling X1). This task ships the
// requests view (plus Blocked, `view=blocked`) and the members view (`view=members`,
// leader or rider; riders get it read-only plus "Leave"). /settings is a later task.
// Design format: leaders get LeaderReview; any other role, including a persisted trip
// with no `viewer`, gets a "Leaders only" notice and NOTHING is requested (UI gate; the
// server enforces). Gated on `viewerRole()` alone, never `isMember`/`access`.
// APIs called: GET /api/v2/trips/{tripId} read from the shell's cache
// (refetchOnMount: false); the review calls live in LeaderReview.
export const Route = createFileRoute("/trips/$tripId/members")({
  validateSearch: (s: Record<string, unknown>): { view: "requests" | "members" | "blocked" } => ({
    view: s.view === "members" || s.view === "blocked" ? s.view : "requests",
  }),
  component: Members,
});

function Members() {
  const { tripId } = Route.useParams();
  const { view } = Route.useSearch();
  const trip = useGetTripApiV2TripsTripIdGet({ tripId }, { query: { refetchOnMount: false } });
  if (!trip.data) return null;
  const role = viewerRole(trip.data);
  if (view === "members" && (role === "leader" || role === "rider")) {
    return <MembersView tripId={tripId} role={role} tripName={trip.data.name} />;
  }
  if (role !== "leader") {
    return (
      <section className="review" style={{ padding: "var(--space-4)" }}>
        <StatusNotice tone="info" title="Leaders only" detail="Only the leaders of this trip can review join requests." />
        <Link to="/trips/$tripId" params={{ tripId }} className="btn btn--secondary btn--md">
          Back to the trip
        </Link>
      </section>
    );
  }
  const shown: ReviewView = view === "blocked" ? "blocked" : "requests";
  return <LeaderReview tripId={tripId} view={shown} />;
}
