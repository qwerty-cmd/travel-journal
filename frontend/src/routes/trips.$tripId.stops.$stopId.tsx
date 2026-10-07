import { type ReactNode, useId, useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useGetTripApiV2TripsTripIdGet } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import { useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet } from "../api/gen/hooks/useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet";
import type { ListPhotosApiV2TripsTripIdStopsStopIdPhotosGetQueryResponse as PhotosResponse } from "../api/gen/types/ListPhotosApiV2TripsTripIdStopsStopIdPhotosGet";
import { useListStopsApiV2TripsTripIdStopsGet } from "../api/gen/hooks/useListStopsApiV2TripsTripIdStopsGet";
import type { StopOut } from "../api/gen/types/StopOut";
import { useMe } from "../auth";
import { Button } from "../components/Button";
import { PhotoGallery } from "../components/PhotoGallery";
import { AddPhotosControl, PickedPreviews, PickStatus, usePhotoPicks } from "../components/PhotoPicker";
import { StatusNotice } from "../components/StatusNotice";
import { formatInstant } from "../format";
import { ImageIcon } from "../icons";
import { getCachedMe } from "../localStore";
import { enqueue, type V2PhotoItem } from "../offline/queue";
import { viewerRole } from "../tripV2";
import "./trips.$tripId.stops.$stopId.css";

// Design feature: stop detail and gallery at /trips/$tripId/stops/$stopId
// (docs/design/screens/stop-detail.md). Read-only for everyone except riders
// and leaders, who can add photos to this stop. Gate: `viewerRole()` rider or
// leader with a known account id (never isMember / access); nobody else gets
// the control, the strip or the picker. A stop still inside the public delay is
// absent from a non-member's stop list, so it reads as "Stop not found", the
// same as an unknown id.
// Adding photos: picked and processed at pick time (shared PhotoPicker), then
// "Save N photos" enqueues one v2 photo entry per pick (tripId, this stopId,
// fresh client id, takenAt; no uploadedBy) under the account id, in one
// enqueue call, clears the strip and returns focus to "Add photos". The gallery
// does not show queued photos: after each send the queue invalidates this
// stop's photos query, which refetches the grid and the "Photos (n)" count.
// Design format: "Back to trip" link, H2 stop name, arrival time
// (formatInstant: reader's zone with a zone label) + " · approximate location"
// for a manual location, notes if any; the photos header row ("Photos (n)" +
// "Add photos"); the picked-photos strip (status, 72 px previews with "Remove
// photo N", "Wait for photos to finish processing." / "Save N photos" +
// "Cancel", "Couldn't save the photos on this device" + "Try again"); then the
// PhotoGallery ("Loading photos…", the envelope message or "Couldn't load
// photos", "No photos yet", with "Add photos" as its action for a writer).
// APIs called: GET /api/v2/trips/{tripId} (shell's cache: viewer.role), GET
// /api/v2/auth/me (useMe, writers only), GET /api/v2/trips/{tripId}/stops (no
// stop-by-id endpoint; usually already cached from the trip page) and, once the
// stop is found, GET /api/v2/trips/{tripId}/stops/{stopId}/photos. The queue
// later sends POST /api/v2/trips/{tripId}/stops/{stopId}/photos.
export const Route = createFileRoute("/trips/$tripId/stops/$stopId")({
  component: StopDetail,
});

type PhotosQuery = ReturnType<typeof useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet<PhotosResponse>>;

function StopDetail() {
  const { tripId, stopId } = Route.useParams();
  const trip = useGetTripApiV2TripsTripIdGet({ tripId }, { query: { refetchOnMount: false } });
  const stops = useListStopsApiV2TripsTripIdStopsGet({ tripId });
  const stop = stops.data?.find((s) => s.id === stopId);
  const photos = useListPhotosApiV2TripsTripIdStopsStopIdPhotosGet({ tripId, stopId }, { query: { enabled: !!stop } });
  const role = trip.data ? viewerRole(trip.data) : undefined;
  const isWriter = role === "rider" || role === "leader";

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

      {isWriter ? <WriterPhotos tripId={tripId} stop={stop} photos={photos} /> : <PhotosSection photos={photos} />}
    </main>
  );
}

/** Mounted only for a rider or leader; without a known account id it stays read-only. */
function WriterPhotos({ tripId, stop, photos }: { tripId: string; stop: StopOut; photos: PhotosQuery }) {
  const me = useMe();
  // A 401 just now means signed out (useMe clears btj.me in an effect, after this render).
  const userId = me.error?.status === 401 ? undefined : (me.data?.id ?? getCachedMe()?.id);
  if (userId === undefined) return <PhotosSection photos={photos} />;
  return <AddPhotos tripId={tripId} stop={stop} userId={userId} photos={photos} />;
}

function AddPhotos({ tripId, stop, userId, photos }: { tripId: string; stop: StopOut; userId: string; photos: PhotosQuery }) {
  const picks = usePhotoPicks();
  const [saving, setSaving] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  const inputId = useId();
  const buttonId = useId();
  const focusAdd = () => document.getElementById(buttonId)?.focus();

  async function save() {
    if (saving || picks.processing > 0 || picks.photos.length === 0) return;
    setSaving(true);
    setSaveFailed(false);
    const items: V2PhotoItem[] = picks.photos.map((p) => ({
      kind: "photo",
      payload: { tripId, stopId: stop.id, stopName: stop.name, data: { id: p.id, takenAt: p.takenAt } },
      file: p.blob,
    }));
    try {
      await enqueue(items, userId);
    } catch {
      setSaving(false);
      setSaveFailed(true);
      return;
    }
    setSaving(false);
    picks.clear();
    focusAdd();
  }

  function cancel() {
    picks.clear();
    setSaveFailed(false);
    focusAdd();
  }

  const n = picks.photos.length;
  const showStrip = n > 0 || picks.processing > 0 || picks.errors.length > 0;
  const strip = showStrip && (
    <div className="stop-photos__strip">
      <PickStatus processing={picks.processing} errors={picks.errors} />
      <PickedPreviews photos={picks.photos} onRemove={picks.remove} addButtonId={buttonId} />
      {saveFailed && (
        <div className="stop-photos__save-error">
          <StatusNotice tone="danger" title="Couldn't save the photos on this device" alert />
          <Button variant="secondary" onClick={save}>
            Try again
          </Button>
        </div>
      )}
      {n > 0 && (
        <div className="stop-photos__actions">
          {picks.processing > 0 && <p className="stop-photos__helper">Wait for photos to finish processing.</p>}
          <Button loading={saving} disabled={saving || picks.processing > 0} onClick={save}>
            {n === 1 ? "Save 1 photo" : `Save ${n} photos`}
          </Button>
          <Button variant="tertiary" onClick={cancel}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );

  return (
    <PhotosSection
      photos={photos}
      addControl={<AddPhotosControl inputId={inputId} onPick={picks.pick} size="md" buttonId={buttonId} />}
      strip={strip}
    />
  );
}

/** The "Photos (n)" header row, the writer's strip, then the gallery or its states. */
function PhotosSection({ photos, addControl, strip }: { photos: PhotosQuery; addControl?: ReactNode; strip?: ReactNode }) {
  const empty = photos.isSuccess && photos.data.length === 0;
  return (
    <section className="stop-photos">
      <div className="stop-photos__head">
        <h3 className="trip__h2">{photos.isSuccess ? `Photos (${photos.data.length})` : "Photos"}</h3>
        {!empty && addControl}
      </div>
      {strip}
      {photos.isPending ? (
        <p>Loading photos…</p>
      ) : photos.isError ? (
        <p>{photos.error.envelope?.error.message ?? "Couldn't load photos"}</p>
      ) : photos.data.length === 0 ? (
        addControl ? (
          <div className="stop-photos__empty">
            <ImageIcon />
            <p>No photos yet</p>
            {addControl}
          </div>
        ) : (
          <p>No photos yet</p>
        )
      ) : (
        <PhotoGallery photos={photos.data} dataUpdatedAt={photos.dataUpdatedAt} refetch={photos.refetch} />
      )}
    </section>
  );
}
