import { createFileRoute, Link, Outlet } from "@tanstack/react-router";
import { useMe, useSlow } from "../auth";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { JoinRequestPanel } from "../components/JoinRequest";
import { StatusNotice } from "../components/StatusNotice";
import { formatDate } from "../format";
import { ArrowLeftIcon, MapPinIcon } from "../icons";
import { isTripV2NotFound, useTripV2, viewerRole } from "../tripV2";
import "./trips.$tripId.css";

// Design feature: shell for every /trips/$tripId screen
// (docs/design/screens/trip-detail.md, decision-log Entry 29). One trip for
// every audience; what renders comes from the server's `viewer.role`, never
// from device storage. A persisted trip renders while offline (Entry 19); only
// "Trip not found" overrides it.
// Design format, first match wins:
//   - "Trip not found" (NOT_FOUND, identical for unknown and private-to-you):
//     full-page EmptyState; the body depends only on the reader's own sign-in
//     state, never on whether the trip exists. The persisted record is cleared.
//   - trip data (fetched or persisted): top bar (Back to `/`, trip name), header
//     (H1 name, "Starts <date> · N riders", visibility badge, role badge for
//     leader / rider / pending), then the child route. A record from before the
//     TripOut extension (no `viewer` / `visibility`) just omits those badges.
//   - any other error with nothing persisted: "Can't reach the server…" + Try again.
//   - otherwise: header skeleton (+ "Waking up the server…" after 3 s).
//     Under the header, a leader-only "Requests" link (gated on viewerRole() alone) to
//     /trips/$tripId/members, then JoinRequestPanel (join flow, gated on viewerRole() alone).
// APIs called: GET /api/v2/trips/{tripId} via useTripV2 (src/tripV2.ts), seeded
// from the per-trip-id persisted TripOut; GET /api/v2/auth/me (useMe) for the
// 404 copy only. Child routes read the same cache entry with refetchOnMount: false.
export const Route = createFileRoute("/trips/$tripId")({
  component: TripShell,
});

const ROLE_BADGE = { leader: "leader", rider: "rider", pending: "pending" } as const;

function TripShell() {
  const { tripId } = Route.useParams();
  const trip = useTripV2(tripId);
  const slow = useSlow(trip.isPending);

  if (isTripV2NotFound(trip.error)) return <TripNotFound />;

  if (trip.data) {
    const t = trip.data;
    const role = viewerRole(t);
    const roleBadge = role && role in ROLE_BADGE ? ROLE_BADGE[role as keyof typeof ROLE_BADGE] : null;
    const visibility = (t as Partial<typeof t>).visibility;
    const riders = typeof t.riderCount === "number" ? ` · ${t.riderCount} ${t.riderCount === 1 ? "rider" : "riders"}` : "";
    return (
      <div className="trip">
        <TopBar title={t.name} />
        <header className="trip__header">
          <h1 className="trip__name">{t.name}</h1>
          <p className="trip__meta">
            Starts {formatDate(t.startDate)}
            {riders}
          </p>
          <div className="trip__badges">
            {visibility && <Badge variant={visibility} />}
            {roleBadge && <Badge variant={roleBadge} />}
          </div>
        </header>
        {role === "leader" && (
          <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "requests" }} className="btn btn--secondary btn--md trip__leader-link">
            Requests
          </Link>
        )}
        <JoinRequestPanel tripId={tripId} role={role} />
        <Outlet />
      </div>
    );
  }

  if (trip.isError) {
    return (
      <div className="trip">
        <TopBar />
        <div className="trip__panel">
          <StatusNotice tone="danger" title="Can't reach the server. Check your signal and try again." />
          <Button variant="secondary" onClick={() => trip.refetch()}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="trip">
      <TopBar />
      <div className="trip__panel" aria-busy="true">
        <span className="visually-hidden">Loading…</span>
        {slow && (
          <StatusNotice tone="info" title="Waking up the server…" detail="The first visit after a quiet spell can take a little while." />
        )}
        <div className="trip__skeleton trip__skeleton--title" />
        <div className="trip__skeleton" />
        <div className="trip__skeleton trip__skeleton--map" />
      </div>
    </div>
  );
}

function TopBar({ title }: { title?: string }) {
  return (
    <div className="trip__topbar">
      <Link to="/" className="trip__back" aria-label="Back">
        <ArrowLeftIcon />
      </Link>
      {title && <span className="trip__topbar-title">{title}</span>}
    </div>
  );
}

function TripNotFound() {
  const signedIn = !!useMe().data;
  return (
    <div className="trip">
      <TopBar />
      <div className="trip__notfound">
        <span className="trip__notfound-icon">
          <MapPinIcon />
        </span>
        <h1 className="trip__notfound-title">Trip not found</h1>
        <p className="trip__muted">
          {signedIn
            ? "Check the link. If this is a private trip, you need to be a member to see it."
            : "Check the link. If this is a private trip, sign in with an account that belongs to it."}
        </p>
      </div>
    </div>
  );
}
