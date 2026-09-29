import { type ChangeEvent, type FormEvent, useEffect, useState } from "react";
import { createFileRoute, Navigate, useNavigate } from "@tanstack/react-router";
import { useGetTripApiTripsSlugGet } from "../api/gen/hooks/useGetTripApiTripsSlugGet";
import type { LocationSource } from "../api/gen/types/LocationSource";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { TripMap } from "../components/TripMap";
import { enqueue, type PhotoItem } from "../offline/queue";
import { getDisplayName } from "../localStore";
import { processPhoto } from "../photo";

// Design feature: add stop (spec Section 6), rider only. Captures a stop (name,
// notes, location, photos) with or without signal. Never POSTs: the stop goes
// into the offline queue (decision-log Entry 19), which sends it when it can.
// A viewer (trip.access !== "rider", from the server) is redirected to the trip
// home before geolocation is ever requested, so viewers never see a GPS
// permission prompt.
// Design format:
//   - Location line: "Getting GPS fix…", then "Location: lat, lng (GPS|map
//     tap)". GPS first (enableHighAccuracy). Any geolocation error, no
//     navigator.geolocation, or no answer within GPS_FALLBACK_MS (15s, our own
//     timer, because the browser's timeout doesn't start until permission is
//     granted, so an ignored prompt would otherwise wait forever) shows "GPS
//     unavailable: tap the map…" and a TripMap whose tap sets locationSource
//     "manual". A GPS fix arriving after the fallback is still used, but never
//     replaces a point the rider already tapped.
//   - Name (required), Notes, Photos (multiple, image/*). Photos are processed
//     at pick time (processPhoto: ≤1600px JPEG + takenAt); each gets its client
//     id when processing finishes; "Processing photos…" while any are in
//     flight; an undecodable file shows "Couldn't read photo <name>" and is not
//     queued. Each picked photo has a Remove button.
//   - "Save stop" is disabled until there is a name and a location and no photo
//     is processing. On save, the stop and its photos are enqueued in one
//     transaction, stop first, then the route navigates to the trip home. A
//     failed local write shows "Couldn't save the stop on this device".
// The stop id and arrivedAt are fixed at mount, so a resubmit replays the same
// idempotent id. arrivedAt is toISOString(): UTC with a "Z" offset, so aware
// but the rider's local offset is not preserved. uploadedBy is the device's
// display name (the trip shell guarantees one is saved).
// APIs called: none directly. GET /api/trips/{slug} is read from the shell's
// cache for `access`. The queue later sends POST /api/trips/{slug}/stops and
// POST /api/trips/{slug}/stops/{stop_id}/photos (see src/offline/queue.ts).
export const Route = createFileRoute("/t/$slug/add")({
  component: AddStop,
});

const GPS_FALLBACK_MS = 15_000;

type Position = { lat: number; lng: number; locationSource: LocationSource };
type Picked = { id: string; fileName: string; blob: Blob; takenAt: string };

function AddStop() {
  const { slug } = Route.useParams();
  // Reads the trip the shell (useTrip) already loaded; no second fetch on mount.
  const trip = useGetTripApiTripsSlugGet({ slug }, { query: { refetchOnMount: false } });
  const navigate = useNavigate();
  const [id] = useState(() => crypto.randomUUID());
  const [arrivedAt] = useState(() => new Date().toISOString());
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [position, setPosition] = useState<Position | null>(null);
  const [gpsFailed, setGpsFailed] = useState(() => !navigator.geolocation);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [photos, setPhotos] = useState<Picked[]>([]);
  const [processing, setProcessing] = useState(0);
  const [photoErrors, setPhotoErrors] = useState<string[]>([]);

  // The trip shell only renders this once the trip is loaded (or cached), so
  // trip.data is the server's access answer, never a device-side guess.
  const isRider = trip.data?.access === "rider";

  // Geolocation is only requested once the rider check passes, so a viewer
  // redirected away never sees a permission prompt. The browser's own timeout
  // does not start until permission is granted, so an ignored or dismissed
  // prompt would wait forever: our own timer falls back to the map tap. A fix
  // that arrives after the fallback is still used, but never over a point the
  // rider already tapped.
  useEffect(() => {
    if (!isRider || !navigator.geolocation) return;
    let active = true;
    const fallback = setTimeout(() => setGpsFailed(true), GPS_FALLBACK_MS);
    navigator.geolocation.getCurrentPosition(
      (p) => {
        if (!active) return;
        clearTimeout(fallback);
        setPosition((cur) => cur ?? { lat: p.coords.latitude, lng: p.coords.longitude, locationSource: "gps" });
      },
      () => {
        if (!active) return;
        clearTimeout(fallback);
        setGpsFailed(true);
      },
      { enableHighAccuracy: true, timeout: GPS_FALLBACK_MS },
    );
    return () => {
      active = false;
      clearTimeout(fallback);
    };
  }, [isRider]);

  if (!isRider) return <Navigate to="/t/$slug" params={{ slug }} replace />;

  function pick(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    setPhotoErrors([]);
    for (const file of files) {
      setProcessing((n) => n + 1);
      processPhoto(file)
        .then(({ blob, takenAt }) =>
          setPhotos((ps) => [...ps, { id: crypto.randomUUID(), fileName: file.name, blob, takenAt }]),
        )
        .catch(() => setPhotoErrors((es) => [...es, `Couldn't read photo ${file.name}`]))
        .finally(() => setProcessing((n) => n - 1));
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!position || saving || processing > 0) return;
    setSaving(true);
    setError(null);
    const data: StopCreate = { id, name: name.trim(), ...position, arrivedAt, notes: notes.trim() || null };
    try {
      const stop = { kind: "stop" as const, payload: { slug, data } };
      // uploadedBy is never null here: the trip shell blocks this route until a name is saved.
      const uploadedBy = getDisplayName()!;
      const photoItems: PhotoItem[] = photos.map((p) => ({
        kind: "photo",
        payload: { slug, stopId: id, stopName: data.name, data: { id: p.id, uploadedBy, takenAt: p.takenAt } },
        file: p.blob,
      }));
      await enqueue(photoItems.length ? [stop, ...photoItems] : stop);
    } catch {
      setSaving(false);
      setError("Couldn't save the stop on this device");
      return;
    }
    navigate({ to: "/t/$slug", params: { slug } });
  }

  return (
    <main style={{ padding: 16 }}>
      <h2>Add stop</h2>
      <form onSubmit={submit}>
        <p>
          {position
            ? `Location: ${position.lat.toFixed(5)}, ${position.lng.toFixed(5)} (${position.locationSource === "gps" ? "GPS" : "map tap"})`
            : gpsFailed
              ? "GPS unavailable: tap the map to set the location"
              : "Getting GPS fix…"}
        </p>
        {gpsFailed && (
          <TripMap onMapClick={(lat, lng) => setPosition({ lat, lng, locationSource: "manual" })} />
        )}
        <label style={{ display: "block" }}>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label style={{ display: "block" }}>
          Notes
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
        <label style={{ display: "block" }}>
          Photos
          <input type="file" accept="image/*" multiple onChange={pick} />
        </label>
        {processing > 0 && <p>Processing photos…</p>}
        {photoErrors.map((m) => (
          <p key={m} role="alert">
            {m}
          </p>
        ))}
        <ul>
          {photos.map((p) => (
            <li key={p.id}>
              {p.fileName}{" "}
              <button type="button" onClick={() => setPhotos((ps) => ps.filter((x) => x.id !== p.id))}>
                Remove
              </button>
            </li>
          ))}
        </ul>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={saving || processing > 0 || !name.trim() || !position}>
          Save stop
        </button>
      </form>
    </main>
  );
}
