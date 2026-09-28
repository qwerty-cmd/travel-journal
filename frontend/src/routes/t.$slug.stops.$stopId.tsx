import { useEffect, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useListPhotosApiTripsSlugStopsStopIdPhotosGet } from "../api/gen/hooks/useListPhotosApiTripsSlugStopsStopIdPhotosGet";
import { useListStopsApiTripsSlugStopsGet } from "../api/gen/hooks/useListStopsApiTripsSlugStopsGet";
import type { PhotoOut } from "../api/gen/types/PhotoOut";

// Stop detail (spec Section 6, screen 3): same for rider and viewer, read-only.
// There is no stop-by-id endpoint, so the stop comes from the GET /stops list
// (usually already cached from the trip home). Photo URLs are presigned and
// short-lived: shown straight from the query, never persisted. Accepted
// limits: a long-open page can outlive its URLs, and photos need the network.
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
  const [open, setOpen] = useState<PhotoOut | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(null);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

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
        {new Date(stop.arrivedAt).toLocaleString()}
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
              onClick={() => setOpen(photo)}
              style={{ padding: 0, border: 0, width: THUMB, height: THUMB }}
            >
              <img
                src={photo.url}
                alt="Stop photo"
                loading="lazy"
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
          onClick={() => setOpen(null)}
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
          <img src={open.url} alt="Stop photo" style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
        </div>
      )}
    </main>
  );
}
