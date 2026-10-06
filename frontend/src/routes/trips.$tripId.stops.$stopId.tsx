import { createFileRoute, Link } from "@tanstack/react-router";
import { useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet } from "../api/gen/hooks/useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet";
import { useListStopsApiV2TripsTripIdStopsGet } from "../api/gen/hooks/useListStopsApiV2TripsTripIdStopsGet";
import { PhotoGallery } from "../components/PhotoGallery";
import { formatInstant } from "../format";

// Design feature: stop detail and gallery at /trips/$tripId/stops/$stopId
// (docs/design/screens/stop-detail.md), read-only for everyone. A stop still
// inside the public delay is absent from a non-member's stop list, so it reads
// as "Stop not found", the same as an unknown id.
// Design format: "Back to trip" link, H2 stop name, arrival time
// (formatInstant: reader's zone with a zone label) + " · approximate location"
// for a manual location, notes if any, then the PhotoGallery ("Loading
// photos…", the envelope message or "Couldn't load photos", "No photos yet").
// APIs called: GET /api/v2/trips/{tripId}/stops (no stop-by-id endpoint; usually
// already cached from the trip page) and, once the stop is found, GET
// /api/v2/trips/{tripId}/stops/{stopId}/photos.
export const Route = createFileRoute("/trips/$tripId/stops/$stopId")({
  component: StopDetail,
});

function StopDetail() {
  const { tripId, stopId } = Route.useParams();
  const stops = useListStopsApiV2TripsTripIdStopsGet({ tripId });
  const stop = stops.data?.find((s) => s.id === stopId);
  const photos = useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet({ tripId, stopId }, { query: { enabled: !!stop } });

  const back = (
    <Link to="/trips/$tripId" params={{ tripId }}>
      Back to trip
    </Link>
  );

  if (stops.isPending) return <p className="trip__panel">Loading stops…</p>;
  if (stops.isError)
    return <p className="trip__panel">{stops.error.envelope?.error.message ?? "Couldn't load stops"}</p>;
  if (!stop)
    return (
      <main className="trip__panel">
        <p>Stop not found</p>
        {back}
      </main>
    );

  return (
    <main className="trip__panel">
      {back}
      <h2 className="trip__h2">{stop.name}</h2>
      <p className="trip__muted">
        {formatInstant(stop.arrivedAt)}
        {stop.locationSource === "manual" && " · approximate location"}
      </p>
      {stop.notes !== null && <p className="trip__notes">{stop.notes}</p>}

      {photos.isPending ? (
        <p>Loading photos…</p>
      ) : photos.isError ? (
        <p>{photos.error.envelope?.error.message ?? "Couldn't load photos"}</p>
      ) : photos.data.length === 0 ? (
        <p>No photos yet</p>
      ) : (
        <PhotoGallery photos={photos.data} dataUpdatedAt={photos.dataUpdatedAt} refetch={photos.refetch} />
      )}
    </main>
  );
}
