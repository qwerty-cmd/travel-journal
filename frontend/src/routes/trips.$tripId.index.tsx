import { useCallback } from "react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useGetMapApiV2TripsTripIdMapGet } from "../api/gen/hooks/useGetMapApiV2TripsTripIdMapGet";
import { useGetTripApiV2TripsTripIdGet } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import { useListStopsApiV2TripsTripIdStopsGet } from "../api/gen/hooks/useListStopsApiV2TripsTripIdStopsGet";
import { Timeline } from "../components/Timeline";
import { TripMap } from "../components/TripMap";
import { delayCaption } from "../tripV2";

// Design feature: the trip's journal at /trips/$tripId (trip-detail.md, Map tab
// content plus the timeline and bikes on one page). Non-members of a public
// trip read it delay-filtered (server-side) and are told so; members see it live.
// Design format: delay caption (DESIGN.md §9: "Stops appear here N hours after
// they're added.", only for non-members of a public trip with N > 0), TripMap
// ("Map unavailable" if the map request fails), "Stops" with the Timeline
// ("Loading stops…", the envelope message or "Couldn't load stops"), then
// "Bikes": one card per bike sorted by rider name, "No bikes yet" when empty.
// Pins and timeline rows open /trips/$tripId/stops/$stopId (the gallery).
// Read-only for every role here: Add stop and bike editing arrive with their
// own routes (t-am-fe-rider-add-stop, t-am-fe-bikes-v2) and must gate on
// `viewer.role` rider or leader.
// APIs called: GET /api/v2/trips/{tripId}/map, GET /api/v2/trips/{tripId}/stops,
// and GET /api/v2/trips/{tripId} read from the shell's cache (refetchOnMount:
// false); bikes come from that TripOut, so they also render from the persisted
// record offline.
export const Route = createFileRoute("/trips/$tripId/")({
  component: TripHome,
});

function TripHome() {
  const { tripId } = Route.useParams();
  const navigate = useNavigate();
  const trip = useGetTripApiV2TripsTripIdGet({ tripId }, { query: { refetchOnMount: false } });
  const map = useGetMapApiV2TripsTripIdMapGet({ tripId });
  const stops = useListStopsApiV2TripsTripIdStopsGet({ tripId });

  const openStop = useCallback(
    (stopId: string) => navigate({ to: "/trips/$tripId/stops/$stopId", params: { tripId, stopId } }),
    [navigate, tripId],
  );

  if (!trip.data) return null;
  const caption = delayCaption(trip.data);
  const bikes = [...(trip.data.bikes ?? [])].sort((a, b) => a.riderName.localeCompare(b.riderName));

  return (
    <main className="trip__panel">
      {caption && <p className="trip__muted">{caption}</p>}
      {map.isError ? <p className="trip__map-error">Map unavailable</p> : <TripMap collection={map.data} onOpenStop={openStop} />}

      <h2 className="trip__h2">Stops</h2>
      {stops.isPending ? (
        <p>Loading stops…</p>
      ) : stops.isError ? (
        <p>{stops.error.envelope?.error.message ?? "Couldn't load stops"}</p>
      ) : (
        <Timeline stops={stops.data} onOpenStop={openStop} />
      )}

      <h2 className="trip__h2">Bikes</h2>
      {bikes.length === 0 ? (
        <p>No bikes yet</p>
      ) : (
        <ul className="trip__bikes">
          {bikes.map((bike) => (
            <li key={bike.id} className="trip__bike">
              <h3 className="trip__bike-rider">{bike.riderName}</h3>
              <p>
                {bike.year} {bike.make} {bike.model}
              </p>
              {bike.specs !== "" && <p className="trip__bike-specs">{bike.specs}</p>}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
