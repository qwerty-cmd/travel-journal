import { useState } from "react";
import { createFileRoute, Outlet } from "@tanstack/react-router";
import { DisplayNamePrompt } from "../components/DisplayNamePrompt";
import { getDisplayName } from "../localStore";
import { isTripNotFound, useTrip } from "../trip";

// Trip shell for every /t/$slug screen (decision-log Entry 18): trip header,
// then the child route. A cached trip renders even when the server can't be
// reached (Entry 19); only a NOT_FOUND envelope overrides it.
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
