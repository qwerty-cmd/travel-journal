import { useNavigate } from "@tanstack/react-router";
import type { StopOut } from "../api/gen/types/StopOut";

// Chronological stop feed (spec Section 6, trip home). GET /stops promises no
// order, so sort here: oldest first by instant (Date.parse, so mixed UTC
// offsets compare correctly — never by string), ties broken by id. Clicking
// an item opens the stop's detail screen.
export function Timeline({ stops }: { stops: StopOut[] }) {
  const navigate = useNavigate();
  if (stops.length === 0) return <p>No stops yet.</p>;

  const sorted = [...stops].sort(
    (a, b) => Date.parse(a.arrivedAt) - Date.parse(b.arrivedAt) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );

  const open = (stopId: string) =>
    navigate({ from: "/t/$slug", to: "/t/$slug/stops/$stopId", params: (prev) => ({ ...prev, stopId }) });

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
              {new Date(stop.arrivedAt).toLocaleString()}
              {stop.locationSource === "manual" && " · approximate location"}
            </div>
            {stop.notes !== null && <p style={{ margin: "4px 0 0" }}>{stop.notes}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}
