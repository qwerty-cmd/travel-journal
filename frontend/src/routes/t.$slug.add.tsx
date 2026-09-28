import { type FormEvent, useEffect, useState } from "react";
import { createFileRoute, Navigate, useNavigate } from "@tanstack/react-router";
import { useGetTripApiTripsSlugGet } from "../api/gen/hooks/useGetTripApiTripsSlugGet";
import type { LocationSource } from "../api/gen/types/LocationSource";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { TripMap } from "../components/TripMap";
import { enqueue } from "../offline/queue";

// Add stop (spec Section 6), rider-only. Never POSTs: the stop goes into the
// offline queue (decision-log Entry 19), which sends it when it can. The id and
// arrivedAt are fixed at mount, so a resubmit replays the same idempotent id.
// arrivedAt is toISOString(): UTC with a "Z" offset, so aware but the rider's
// local offset is not preserved. GPS first; any geolocation failure falls back
// to tapping the map (locationSource "manual").
export const Route = createFileRoute("/t/$slug/add")({
  component: AddStop,
});

type Position = { lat: number; lng: number; locationSource: LocationSource };

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

  useEffect(() => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (p) => setPosition({ lat: p.coords.latitude, lng: p.coords.longitude, locationSource: "gps" }),
      () => setGpsFailed(true),
      { enableHighAccuracy: true, timeout: 15_000 },
    );
  }, []);

  // The trip shell only renders this once the trip is loaded (or cached), so
  // trip.data is the server's access answer, never a device-side guess.
  if (trip.data?.access !== "rider") return <Navigate to="/t/$slug" params={{ slug }} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!position || saving) return;
    setSaving(true);
    setError(null);
    const data: StopCreate = { id, name: name.trim(), ...position, arrivedAt, notes: notes.trim() || null };
    try {
      await enqueue({ kind: "stop", payload: { slug, data } });
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
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={saving || !name.trim() || !position}>
          Save stop
        </button>
      </form>
    </main>
  );
}
