import { useNavigate } from "@tanstack/react-router";
import type { StopOut } from "../api/gen/types/StopOut";
import { formatInstant } from "../format";

// Design feature: chronological stop feed on the trip home (spec Section 6).
// Clicking (or Enter on) an item opens that stop's detail screen.
// Design format: prop `stops: StopOut[]`; optional `onOpenStop(stopId)` replaces
// the default /t/$slug/stops/$stopId navigation (used by /trips/$tripId). Empty → "No stops yet.". Otherwise an
// unstyled list, one row per stop: bold name; arrival time via formatInstant
// (reader's zone, with a zone label) plus " · approximate location" for a
// manual (map-tap) location; notes if not null. GET /stops promises no order,
// so sort here: oldest first by instant (Date.parse, so mixed UTC offsets
// compare correctly — never by string), ties broken by id.
// APIs called: none itself. The trip home passes the result of
// GET /api/trips/{slug}/stops.
export function Timeline({ stops, onOpenStop }: { stops: StopOut[]; onOpenStop?: (stopId: string) => void }) {
  const navigate = useNavigate();
  if (stops.length === 0) return <p>No stops yet.</p>;

  const sorted = [...stops].sort(
    (a, b) => Date.parse(a.arrivedAt) - Date.parse(b.arrivedAt) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );

  const open =
    onOpenStop ??
    ((stopId: string) =>
      navigate({ from: "/t/$slug", to: "/t/$slug/stops/$stopId", params: (prev) => ({ ...prev, stopId }) }));

  return (
    <ol style={{ listStyle: "none", padding: 0 }}>
      {sorted.map((stop) => (
        <li key={stop.id} style={{ padding: "8px 0", borderBottom: "1px solid #ddd" }}>
          <div
            role="link"
            tabIndex={0}
            onClick={() => open(stop.id)}
            onKeyDown={(e) => e.key === "Enter" && open(stop.id)}
            style={{ cursor: "pointer" }}
          >
            <strong>{stop.name}</strong>
            <div>
              {formatInstant(stop.arrivedAt)}
              {stop.locationSource === "manual" && " · approximate location"}
            </div>
            {stop.notes !== null && <p style={{ margin: "4px 0 0" }}>{stop.notes}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}
