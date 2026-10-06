import { type FormEvent, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { getTripApiV2TripsTripIdGetQueryKey, useGetTripApiV2TripsTripIdGet } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import {
  listBikesApiV2TripsTripIdBikesGetQueryKey,
  useListBikesApiV2TripsTripIdBikesGet,
} from "../api/gen/hooks/useListBikesApiV2TripsTripIdBikesGet";
import { useCreateBikeApiV2TripsTripIdBikesPost } from "../api/gen/hooks/useCreateBikeApiV2TripsTripIdBikesPost";
import { usePatchBikeApiV2TripsTripIdBikesBikeIdPatch } from "../api/gen/hooks/usePatchBikeApiV2TripsTripIdBikesBikeIdPatch";
import type { BikeOut } from "../api/gen/types/BikeOut";
import type { BikePatch } from "../api/gen/types/BikePatch";
import { errorText, formatRetry, isOffline, isRateLimited, useRetryCountdown, useSingleFlight } from "../auth";
import { Button } from "../components/Button";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";
import { viewerRole } from "../tripV2";
import "./trips.$tripId.bikes.css";

// Design feature: Bikes tab at /trips/$tripId/bikes (docs/design/screens/trip-detail.md).
// Design format: one card per bike (rider name, "year make model", specs with line
// breaks kept) sorted by rider name, for everyone who can read the trip; "No bikes
// yet" when empty. Only viewerRole() rider or leader (never isMember / access) gets
// the "Add bike" button, an Edit button per card, and the inline form. Required:
// rider name, make, model, integer year; specs optional. Writes are online-only and
// never queued: offline shows "You're offline — try again when connected." and
// sends nothing. Errors sit under the form with the input kept: 403 danger envelope
// message (+ trip refetch so controls re-render), 401 warning with a Sign in link
// (no redirect), 422 danger envelope message, 429 warning with a Retry-After countdown.
// APIs called: GET /api/v2/trips/{tripId} (shell cache, refetchOnMount: false; also
// the offline fallback for the list); GET /api/v2/trips/{tripId}/bikes;
// POST /api/v2/trips/{tripId}/bikes (client id fixed per form, so a retry replays);
// PATCH /api/v2/trips/{tripId}/bikes/{bikeId} (changed fields only, never null).
export const Route = createFileRoute("/trips/$tripId/bikes")({
  component: Bikes,
});

const OFFLINE = "You're offline — try again when connected.";

function Bikes() {
  const { tripId } = Route.useParams();
  const trip = useGetTripApiV2TripsTripIdGet({ tripId }, { query: { refetchOnMount: false } });
  const list = useListBikesApiV2TripsTripIdBikesGet({ tripId });
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  // Bumped after a successful create: remounts the add form with a fresh id.
  const [addKey, setAddKey] = useState(0);
  if (!trip.data) return null;

  const role = viewerRole(trip.data);
  const canWrite = role === "rider" || role === "leader";
  const bikes = [...(list.data ?? trip.data.bikes ?? [])].sort((a, b) => a.riderName.localeCompare(b.riderName));

  return (
    <section className="bikes">
      <h2>Bikes</h2>
      {isRateLimited(list.error) && <StatusNotice tone="warning" title="Too many requests." detail={retryText(list.error)} />}
      {canWrite && !adding && (
        <Button variant="secondary" onClick={() => setAdding(true)}>
          Add bike
        </Button>
      )}
      {canWrite && adding && (
        <BikeForm
          key={addKey}
          tripId={tripId}
          onDone={() => {
            setAdding(false);
            setAddKey((k) => k + 1);
          }}
        />
      )}
      {bikes.length === 0 ? (
        <p>No bikes yet</p>
      ) : (
        <ul className="bikes__list">
          {bikes.map((bike) => (
            <li key={bike.id} className="bikes__card">
              {canWrite && editing === bike.id ? (
                <BikeForm tripId={tripId} bike={bike} onDone={() => setEditing(null)} />
              ) : (
                <>
                  <h3 className="bikes__rider">{bike.riderName}</h3>
                  <p>
                    {bike.year} {bike.make} {bike.model}
                  </p>
                  {bike.specs !== "" && <p className="bikes__specs">{bike.specs}</p>}
                  {canWrite && (
                    <Button variant="secondary" onClick={() => setEditing(bike.id)}>
                      Edit
                    </Button>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <Link to="/trips/$tripId" params={{ tripId }} className="btn btn--tertiary btn--md">
        Back to the trip
      </Link>
    </section>
  );
}

function retryText(e: unknown): string | undefined {
  const s = (e as { retryAfter?: number }).retryAfter;
  return s === undefined ? undefined : formatRetry(s);
}

// Create when `bike` is absent, edit otherwise.
function BikeForm({ tripId, bike, onDone }: { tripId: string; bike?: BikeOut; onDone: () => void }) {
  const queryClient = useQueryClient();
  // Fixed at mount: a resubmit after a failure replays the same idempotent id.
  const [id] = useState(() => crypto.randomUUID());
  const [riderName, setRiderName] = useState(bike?.riderName ?? "");
  const [make, setMake] = useState(bike?.make ?? "");
  const [model, setModel] = useState(bike?.model ?? "");
  const [year, setYear] = useState(bike ? String(bike.year) : "");
  const [specs, setSpecs] = useState(bike?.specs ?? "");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [offline, setOffline] = useState(false);
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  // Hook-level onSuccess: a per-mutate() callback is skipped once the form unmounts.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: listBikesApiV2TripsTripIdBikesGetQueryKey({ tripId }) });
    void queryClient.invalidateQueries({ queryKey: getTripApiV2TripsTripIdGetQueryKey({ tripId }) });
  };
  const create = useCreateBikeApiV2TripsTripIdBikesPost({ mutation: { onSuccess: refresh } });
  const patch = usePatchBikeApiV2TripsTripIdBikesBikeIdPatch({ mutation: { onSuccess: refresh } });
  const pending = create.isPending || patch.isPending;

  const yearNum = Number(year);
  const required = (v: string) => (v.trim() ? undefined : "Required.");
  const yearErr = year.trim() === "" || !Number.isInteger(yearNum) ? "Enter a year." : undefined;
  const status = (error as { status?: number } | null)?.status;
  const limited = countdown.remaining > 0;

  function submit(e: FormEvent) {
    e.preventDefault();
    setTried(true);
    setError(null);
    if (required(riderName) || required(make) || required(model) || yearErr || limited) return;
    const fields = { riderName: riderName.trim(), make: make.trim(), model: model.trim(), year: yearNum, specs };
    let body: BikePatch = {};
    if (bike) {
      // Only fields that differ from the bike the form opened with; never null.
      for (const k of Object.keys(fields) as (keyof typeof fields)[]) {
        if (fields[k] !== bike[k]) Object.assign(body, { [k]: fields[k] });
      }
      if (Object.keys(body).length === 0) return onDone();
    }
    // Online-only: nothing is queued; offline just says so.
    if (!navigator.onLine) {
      setOffline(true);
      return;
    }
    setOffline(false);
    const callbacks = {
      onSuccess: () => onDone(),
      onError: (err: unknown) => {
        setError(err);
        countdown.startFrom(err);
        if ((err as { status?: number }).status === 403) {
          void queryClient.invalidateQueries({ queryKey: getTripApiV2TripsTripIdGetQueryKey({ tripId }) });
        }
      },
    };
    flight.run((done) => {
      const settled = { ...callbacks, onSettled: done };
      if (bike) patch.mutate({ tripId, bikeId: bike.id, data: body }, settled);
      else create.mutate({ tripId, data: { id, ...fields } }, settled);
    });
  }

  return (
    <form className="bikes__form" noValidate onSubmit={submit}>
      <TextField label="Rider name" name="riderName" autoComplete="off" value={riderName} onChange={(e) => setRiderName(e.target.value)} error={tried ? required(riderName) : undefined} />
      <TextField label="Make" name="make" autoComplete="off" value={make} onChange={(e) => setMake(e.target.value)} error={tried ? required(make) : undefined} />
      <TextField label="Model" name="model" autoComplete="off" value={model} onChange={(e) => setModel(e.target.value)} error={tried ? required(model) : undefined} />
      <TextField label="Year" name="year" inputMode="numeric" autoComplete="off" value={year} onChange={(e) => setYear(e.target.value)} error={tried ? yearErr : undefined} />
      <div className="field">
        <label className="field__label" htmlFor={`specs-${id}`}>
          Specs
        </label>
        <textarea id={`specs-${id}`} className="field__input bikes__textarea" value={specs} onChange={(e) => setSpecs(e.target.value)} />
      </div>
      {offline || isOffline(error) ? (
        <StatusNotice tone="danger" alert title={OFFLINE} />
      ) : status === 401 ? (
        <>
          <StatusNotice tone="warning" alert title="Your session has ended. Sign in, then save again." />
          <Link to="/signin" search={{ next: `/trips/${tripId}/bikes` }} className="btn btn--secondary btn--md">
            Sign in
          </Link>
        </>
      ) : isRateLimited(error) ? (
        limited ? <StatusNotice tone="warning" title="Too many requests." detail={formatRetry(countdown.remaining)} /> : null
      ) : error ? (
        <StatusNotice tone="danger" alert title={errorText(error, OFFLINE)} />
      ) : null}
      <div className="bikes__actions">
        <Button type="submit" loading={pending} loadingLabel="Saving…" disabled={limited}>
          {bike ? "Save bike" : "Add bike"}
        </Button>
        <Button variant="tertiary" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
