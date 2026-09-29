import { type ChangeEvent, type FormEvent, useEffect, useState } from "react";
import { createFileRoute, Navigate, useNavigate } from "@tanstack/react-router";
import { useGetTripApiTripsSlugGet } from "../api/gen/hooks/useGetTripApiTripsSlugGet";
import type { LocationSource } from "../api/gen/types/LocationSource";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { TripMap } from "../components/TripMap";
import { enqueue, type PhotoItem } from "../offline/queue";
import { getDisplayName } from "../localStore";
import { processPhoto } from "../photo";

// Add stop (spec Section 6), rider-only. Never POSTs: the stop goes into the
// offline queue (decision-log Entry 19), which sends it when it can. The id and
// arrivedAt are fixed at mount, so a resubmit replays the same idempotent id.
// arrivedAt is toISOString(): UTC with a "Z" offset, so aware but the rider's
// local offset is not preserved. GPS first; any geolocation failure, or no
// answer within 15s, falls back to tapping the map (locationSource "manual"). Photos are processed at pick
// time (id fixed when processing finishes, before submit) and enqueued in the
// same call as the stop, stop first.
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
