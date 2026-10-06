import { useEffect, useRef, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useListPhotosApiTripsSlugStopsStopIdPhotosGet } from "../api/gen/hooks/useListPhotosApiTripsSlugStopsStopIdPhotosGet";
import { useListStopsApiTripsSlugStopsGet } from "../api/gen/hooks/useListStopsApiTripsSlugStopsGet";
import { formatInstant } from "../format";
import "./t.$slug.css";

// Design feature: stop detail (spec Section 6, screen 3). One stop with its
// photos; same for rider and viewer, read-only.
// Design format: "Back to trip" link, stop name, arrival time (formatInstant,
// in the reader's zone with a zone label) plus " · approximate location" for a
// manual (map-tap) location, notes if any, then the photos: a wrap of 96px
// square lazy-loaded thumbnails ("Loading photos…", the envelope message or
// "Couldn't load photos", or "No photos yet"). Tapping a thumbnail opens a
// full-screen dialog; a click anywhere or Escape closes it. An unknown stop id
// shows "Stop not found" with a back link.
// Photo URLs are presigned and short-lived (1h): shown straight from the query,
// never persisted. An <img> onError (typically an expired URL on a long-open
// page) refetches the photo list once for fresh URLs; a list that is itself
// the result of that refetch never triggers another, so a genuinely broken
// photo can't loop, and a later ordinary refetch re-arms it. The enlarged view
// is looked up by id, so it picks up the fresh URL too. Accepted limit: photos
// need the network.
// APIs called: GET /api/trips/{slug}/stops (useListStopsApiTripsSlugStopsGet;
// there is no stop-by-id endpoint, and the list is usually already cached from
// the trip home) and, once the stop is found, GET
// /api/trips/{slug}/stops/{stop_id}/photos
// (useListPhotosApiTripsSlugStopsStopIdPhotosGet).
export const Route = createFileRoute("/t/$slug/stops/$stopId")({
  component: StopDetail,
});


function StopDetail() {
  const { slug, stopId } = Route.useParams();
  const stops = useListStopsApiTripsSlugStopsGet({ slug });
  const stop = stops.data?.find((s) => s.id === stopId);
  const photos = useListPhotosApiTripsSlugStopsStopIdPhotosGet(
    { slug, stop_id: stopId },
    { query: { enabled: !!stop } },
  );
  const [openId, setOpenId] = useState<string | null>(null);
  const open = photos.data?.find((p) => p.id === openId);

  // One refetch per load error. A list that is itself the result of such a
  // refetch never triggers another, so a photo that is genuinely broken can't
  // loop; a later ordinary refetch (e.g. on window focus) re-arms it.
  const refresh = useRef<{ busy: boolean; resultAt: number | null }>({ busy: false, resultAt: null });
  function onPhotoError() {
    const r = refresh.current;
    if (r.busy || photos.dataUpdatedAt === r.resultAt) return;
    r.busy = true;
    photos.refetch().then((res) => {
      r.busy = false;
      r.resultAt = res.dataUpdatedAt;
    });
  }

  useEffect(() => {
    if (!openId) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpenId(null);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [openId]);

  if (stops.isPending) return <p className="legacy__status">Loading stops…</p>;
  if (stops.isError)
    return <p className="legacy__status">{stops.error.envelope?.error.message ?? "Couldn't load stops"}</p>;
  if (!stop)
    return (
      <main className="legacy__panel">
        <p>Stop not found</p>
        <Link to="/t/$slug" params={{ slug }} className="btn btn--tertiary btn--md">
          Back to trip
        </Link>
      </main>
    );

  return (
    <main className="legacy__panel">
      <Link to="/t/$slug" params={{ slug }} className="btn btn--tertiary btn--md">
        Back to trip
      </Link>
      <h2 className="legacy__h2">{stop.name}</h2>
      <p>
        {formatInstant(stop.arrivedAt)}
        {stop.locationSource === "manual" && " · approximate location"}
      </p>
      {stop.notes !== null && <p>{stop.notes}</p>}

      {photos.isPending ? (
        <p>Loading photos…</p>
      ) : photos.isError ? (
        <p>{photos.error.envelope?.error.message ?? "Couldn't load photos"}</p>
      ) : photos.data.length === 0 ? (
        <p>No photos yet</p>
      ) : (
        <div className="legacy__photos">
          {photos.data.map((photo) => (
            <button
              key={photo.id}
              type="button"
              onClick={() => setOpenId(photo.id)}
              className="legacy__thumb"
            >
              <img
                src={photo.url}
                alt="Stop photo"
                loading="lazy"
                onError={onPhotoError}
                className="legacy__thumb-img"
              />
            </button>
          ))}
        </div>
      )}

      {open && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Photo"
          onClick={() => setOpenId(null)}
          className="legacy__viewer"
        >
          <img
            src={open.url}
            alt="Stop photo"
            onError={onPhotoError}
            className="legacy__viewer-img"
          />
        </div>
      )}
    </main>
  );
}
