import { type FormEvent, useCallback, useEffect, useId, useState } from "react";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useGetTripApiV2TripsTripIdGet } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import type { LocationSource } from "../api/gen/types/LocationSource";
import type { StopCreate } from "../api/gen/types/StopCreate";
import { useMe } from "../auth";
import { BottomActionBar } from "../components/BottomActionBar";
import { Button } from "../components/Button";
import { Dialog, DialogActions } from "../components/Dialog";
import { AddPhotosControl, PickedPreviews, PickStatus, usePhotoPicks } from "../components/PhotoPicker";
import { LocationStatus, type LocationState } from "../components/LocationStatus";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";
import { TripMap } from "../components/TripMap";
import { GlobeIcon, XIcon } from "../icons";
import { getCachedMe } from "../localStore";
import { enqueue, type V2PhotoItem, type V2StopItem } from "../offline/queue";
import { viewerRole } from "../tripV2";
import "./trips.$tripId.add.css";

// Design feature: add stop on a v2 trip (docs/design/screens/add-stop.md,
// decision-log Entry 29). The rider's core job, done outdoors, often with no
// signal: it never POSTs, it saves to the offline queue (Entry 19) and returns.
// Behaviour is ported unchanged from /t/$slug/add: the stop id and arrivedAt
// are fixed at mount; GPS first (enableHighAccuracy), with our own 15 s timer
// falling back to the map (the browser's timeout only starts once permission
// is granted); a late GPS fix never replaces a point set on the map; photos are
// processed at pick time (≤ 1600 px JPEG, client id on completion); Save needs
// a name, a location and no photo processing; the stop and its photos are
// enqueued in one transaction, stop first.
// Gate: `viewerRole()` rider or leader only (never isMember / access: a
// pre-extension cached trip has no role and gets no form). Anyone else, and a
// member with no known account id, gets a notice and no form, and geolocation
// is never requested for them. Entries are v2 `{tripId}` payloads stamped
// with the account id from useMe (falling back to the cached `btj.me`, unless
// the server just answered 401); photo entries carry no uploadedBy.
// Design format: close "Cancel" (ConfirmDialog "Discard this stop?" once
// anything is entered) + H1 "Add stop"; LocationStatus; in fallback/manual
// the picker map (crosshair, approximate pin, keyboard "Use map centre",
// DESIGN.md C15) + no-signal caption; Name, Notes, Photos (72 px previews,
// 48 px "Remove photo N"); the public-trip delay note; BottomActionBar with
// the disabled reason and "Save stop". Save failure: "Couldn't save the stop
// on this device".
// APIs called: none directly. GET /api/v2/trips/{tripId} is read from the
// shell's cache (role, visibility, publicDelayHours); GET /api/v2/auth/me via
// useMe. The queue later sends POST /api/v2/trips/{tripId}/stops and
// POST /api/v2/trips/{tripId}/stops/{stopId}/photos.
export const Route = createFileRoute("/trips/$tripId/add")({
  component: AddStop,
});

const GPS_FALLBACK_MS = 15_000;

type Position = { lat: number; lng: number; locationSource: LocationSource };

function AddStop() {
  const { tripId } = Route.useParams();
  const trip = useGetTripApiV2TripsTripIdGet({ tripId }, { query: { refetchOnMount: false } });
  const me = useMe();
  const role = trip.data ? viewerRole(trip.data) : undefined;
  const isWriter = role === "rider" || role === "leader";
  // A 401 just now means signed out (useMe clears btj.me in an effect, after this render).
  const userId = me.error?.status === 401 ? undefined : (me.data?.id ?? getCachedMe()?.id);
  const canAdd = isWriter && userId !== undefined;

  if (!trip.data) return null;
  if (!isWriter) {
    return (
      <Notice tripId={tripId} title="Only riders on this trip can add stops." detail={role === "pending" ? "You'll be able to add stops once a leader approves your request." : undefined} />
    );
  }
  if (!canAdd) {
    return <Notice tripId={tripId} title="Sign in to add a stop." signIn />;
  }
  return <AddStopForm tripId={tripId} userId={userId} visibility={trip.data.visibility} delayHours={trip.data.publicDelayHours} />;
}

function Notice({ tripId, title, detail, signIn }: { tripId: string; title: string; detail?: string; signIn?: boolean }) {
  return (
    <main className="add">
      <StatusNotice tone="info" title={title} detail={detail} />
      {signIn && (
        <Link to="/signin" className="btn btn--primary btn--md">
          Sign in
        </Link>
      )}
      <Link to="/trips/$tripId" params={{ tripId }} className="btn btn--secondary btn--md">
        Back to the trip
      </Link>
    </main>
  );
}

function AddStopForm({
  tripId,
  userId,
  visibility,
  delayHours,
}: {
  tripId: string;
  userId: string;
  visibility: string | undefined;
  delayHours: number;
}) {
  const navigate = useNavigate();
  const [id] = useState(() => crypto.randomUUID());
  const [arrivedAt] = useState(() => new Date().toISOString());
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [position, setPosition] = useState<Position | null>(null);
  const [gpsFailed, setGpsFailed] = useState(() => !navigator.geolocation);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { photos, processing, errors: photoErrors, pick, remove } = usePhotoPicks();
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const notesId = useId();
  const photosId = useId();

  // Mounted only for a rider or leader, so nobody else is ever asked for geolocation.
  useEffect(() => {
    if (!navigator.geolocation) return;
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
  }, []);

  // A tap and "Use map centre" set the same manual location.
  const setManual = useCallback((lat: number, lng: number) => setPosition({ lat, lng, locationSource: "manual" }), []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!position || saving || processing > 0 || !name.trim()) return;
    setSaving(true);
    setError(null);
    const data: StopCreate = { id, name: name.trim(), ...position, arrivedAt, notes: notes.trim() || null };
    try {
      const stop: V2StopItem = { kind: "stop", payload: { tripId, data } };
      const photoItems: V2PhotoItem[] = photos.map((p) => ({
        kind: "photo",
        payload: { tripId, stopId: id, stopName: data.name, data: { id: p.id, takenAt: p.takenAt } },
        file: p.blob,
      }));
      await enqueue(photoItems.length ? [stop, ...photoItems] : stop, userId);
    } catch {
      setSaving(false);
      setError("Couldn't save the stop on this device");
      return;
    }
    navigate({ to: "/trips/$tripId", params: { tripId } });
  }

  const leave = () => navigate({ to: "/trips/$tripId", params: { tripId } });
  const dirty = name !== "" || notes !== "" || photos.length > 0 || position?.locationSource === "manual";
  const location: LocationState = position
    ? { kind: position.locationSource === "gps" ? "gps" : "manual", lat: position.lat, lng: position.lng }
    : gpsFailed
      ? { kind: "fallback" }
      : { kind: "pending" };
  const reason =
    !name.trim() || !position
      ? "Add a name and a location to save."
      : processing > 0
        ? "Wait for photos to finish processing."
        : undefined;
  const note =
    visibility !== "public"
      ? null
      : delayHours > 0
        ? `This stop is visible to the public ${delayHours} ${delayHours === 1 ? "hour" : "hours"} after it's saved.`
        : "This stop is visible to the public as soon as it's sent.";

  return (
    <main className="add">
      <div className="add__bar">
        <button type="button" className="add__close" aria-label="Cancel" onClick={() => (dirty ? setConfirmDiscard(true) : leave())}>
          <XIcon />
        </button>
        <h2 className="add__title">Add stop</h2>
      </div>
      <form className="add__form" onSubmit={submit} noValidate>
        <LocationStatus state={location} />
        {gpsFailed && position?.locationSource !== "gps" && (
          <div className="add__map">
            <TripMap onMapClick={setManual} onUseCentre={setManual} approxPoint={position} />
            <p className="add__muted">With no signal the map may look blank. Tap as close as you can.</p>
          </div>
        )}
        <TextField label="Name" helper="e.g. Roadhouse fuel stop" value={name} onChange={(e) => setName(e.target.value)} required />
        <div className="add__field">
          <label className="add__label" htmlFor={notesId}>
            Notes
          </label>
          <textarea id={notesId} className="add__textarea" value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>
        <section className="add__photos">
          <h3 id={`${photosId}-h`} className="add__label">
            Photos
          </h3>
          <AddPhotosControl inputId={photosId} onPick={pick} size="lg" />
          <PickStatus processing={processing} errors={photoErrors} />
          <PickedPreviews photos={photos} onRemove={remove} />
        </section>
        {note && (
          <p className="add__note">
            <GlobeIcon size="sm" />
            {note}
          </p>
        )}
        {error && (
          <div className="add__save-error">
            <StatusNotice tone="danger" title={error} alert />
          </div>
        )}
        <BottomActionBar helper={reason}>
          <Button type="submit" size="lg" loading={saving} disabled={saving || reason !== undefined}>
            Save stop
          </Button>
        </BottomActionBar>
      </form>
      <Dialog open={confirmDiscard} onClose={() => setConfirmDiscard(false)} titleId="discard-title">
        <h2 id="discard-title" className="add__title">
          Discard this stop?
        </h2>
        <DialogActions>
          <Button variant="danger" onClick={leave}>
            Discard
          </Button>
          <Button variant="secondary" onClick={() => setConfirmDiscard(false)}>
            Keep editing
          </Button>
        </DialogActions>
      </Dialog>
    </main>
  );
}
