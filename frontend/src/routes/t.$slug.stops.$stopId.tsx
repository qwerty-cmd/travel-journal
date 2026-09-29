import { useEffect, useRef, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useListPhotosApiTripsSlugStopsStopIdPhotosGet } from "../api/gen/hooks/useListPhotosApiTripsSlugStopsStopIdPhotosGet";
import { useListStopsApiTripsSlugStopsGet } from "../api/gen/hooks/useListStopsApiTripsSlugStopsGet";
import { formatInstant } from "../format";

// Stop detail (spec Section 6, screen 3): same for rider and viewer, read-only.
// There is no stop-by-id endpoint, so the stop comes from the GET /stops list
// (usually already cached from the trip home). Photo URLs are presigned and
// short-lived (1h): shown straight from the query, never persisted. An image
// that fails to load (typically an expired URL on a long-open page) refetches
// the list once for fresh URLs; the enlarged view is looked up by id, so it
// picks up the fresh URL too. Accepted limit: photos need the network.
export const Route = createFileRoute("/t/$slug/stops/$stopId")({
  component: StopDetail,
});

const THUMB = 96;

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

  if (stops.isPending) return <p style={{ padding: 16 }}>Loading stops…</p>;
  if (stops.isError)
    return <p style={{ padding: 16 }}>{stops.error.envelope?.error.message ?? "Couldn't load stops"}</p>;
  if (!stop)
    return (
      <main style={{ padding: 16 }}>
        <p>Stop not found</p>
        <Link to="/t/$slug" params={{ slug }}>
          Back to trip
        </Link>
      </main>
    );

  return (
    <main style={{ padding: 16 }}>
      <Link to="/t/$slug" params={{ slug }}>
        Back to trip
      </Link>
      <h2>{stop.name}</h2>
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
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {photos.data.map((photo) => (
            <button
              key={photo.id}
              type="button"
              onClick={() => setOpenId(photo.id)}
              style={{ padding: 0, border: 0, width: THUMB, height: THUMB }}
            >
              <img
                src={photo.url}
                alt="Stop photo"
                loading="lazy"
                onError={onPhotoError}
                style={{ width: THUMB, height: THUMB, objectFit: "cover", display: "block" }}
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
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.9)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
        >
          <img
            src={open.url}
            alt="Stop photo"
            onError={onPhotoError}
            style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
          />
        </div>
      )}
    </main>
  );
}
