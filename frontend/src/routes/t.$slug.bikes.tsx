import { type FormEvent, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ApiError } from "../api/client";
import {
  getTripApiTripsSlugGetQueryKey,
  useGetTripApiTripsSlugGet,
} from "../api/gen/hooks/useGetTripApiTripsSlugGet";
import { useCreateBikeApiTripsSlugBikesPost } from "../api/gen/hooks/useCreateBikeApiTripsSlugBikesPost";
import { usePatchBikeApiTripsSlugBikesIdPatch } from "../api/gen/hooks/usePatchBikeApiTripsSlugBikesIdPatch";
import type { BikeOut } from "../api/gen/types/BikeOut";
import type { BikePatch } from "../api/gen/types/BikePatch";

// Design feature: Bikes page (spec Section 6) — every bike on the trip, same
// list for rider and viewer; a rider can also add and edit bikes.
// Design format: back link, then one card per bike (rider name, make, model,
// year, specs with line breaks kept), sorted by rider name — TripOut.bikes
// order is not contract ("match bikes by id"), so the client sorts. Empty
// specs ("") render no specs block; no bikes → "No bikes yet". Rider only
// (trip.access === "rider"): an "Add bike" form and an "Edit" button per bike
// that swaps the card for the same form. Required: rider name, make, model,
// integer year; specs optional. A failed save keeps the input and shows the
// envelope message, an offline message (no response), or a generic one.
// APIs called: GET /api/trips/{slug} (TripOut.bikes) from the cache the trip
// shell (useTrip) already loaded, so the list works from a persisted trip
// offline; POST /api/trips/{slug}/bikes (useCreateBikeApiTripsSlugBikesPost,
// id fixed per form so a retry replays idempotently) and PATCH
// /api/trips/{slug}/bikes/{id} (usePatchBikeApiTripsSlugBikesIdPatch, only
// changed fields; specs cleared with "", never null). Either write refetches
// the trip. Bike writes are online-only direct mutations, not queued: the
// offline queue kinds stay stop | photo (decision-log Entry 19) — bikes are
// entered before the trip, while riders are online.
export const Route = createFileRoute("/t/$slug/bikes")({
  component: Bikes,
});

function Bikes() {
  const { slug } = Route.useParams();
  // Reads the trip the shell (useTrip) already loaded; no second fetch on mount.
  const trip = useGetTripApiTripsSlugGet({ slug }, { query: { refetchOnMount: false } });
  const bikes = [...(trip.data?.bikes ?? [])].sort((a, b) => a.riderName.localeCompare(b.riderName));
  const isRider = trip.data?.access === "rider";
  const [editing, setEditing] = useState<string | null>(null);
  // Bumped after a successful create: remounts the add form, which resets it with a fresh id.
  const [addKey, setAddKey] = useState(0);

  return (
    <main style={{ padding: 16 }}>
      <Link to="/t/$slug" params={{ slug }}>
        Back to trip
      </Link>
      <h2>Bikes</h2>
      {bikes.length === 0 ? (
        <p>No bikes yet</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0 }}>
          {bikes.map((bike) => (
            <li key={bike.id} style={{ marginBottom: 16 }}>
              {isRider && editing === bike.id ? (
                <BikeForm slug={slug} bike={bike} onDone={() => setEditing(null)} />
              ) : (
                <>
                  <h3 style={{ margin: 0 }}>{bike.riderName}</h3>
                  <p style={{ margin: 0 }}>
                    {bike.make} {bike.model} ({bike.year})
                  </p>
                  {bike.specs !== "" && <p style={{ whiteSpace: "pre-wrap" }}>{bike.specs}</p>}
                  {isRider && (
                    <button type="button" onClick={() => setEditing(bike.id)}>
                      Edit
                    </button>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {isRider && (
        <>
          <h3>Add bike</h3>
          <BikeForm key={addKey} slug={slug} onDone={() => setAddKey((k) => k + 1)} />
        </>
      )}
    </main>
  );
}

function errorMessage(e: ApiError): string {
  if (e.envelope) return e.envelope.error.message;
  if (e.status === undefined) return "You're offline — try again when connected.";
  return "Couldn't save the bike — try again.";
}

// Create when `bike` is absent, edit otherwise.
function BikeForm({ slug, bike, onDone }: { slug: string; bike?: BikeOut; onDone: () => void }) {
  const queryClient = useQueryClient();
  // Fixed at mount: a resubmit after a failure replays the same idempotent id.
  const [id] = useState(() => crypto.randomUUID());
  const [riderName, setRiderName] = useState(bike?.riderName ?? "");
  const [make, setMake] = useState(bike?.make ?? "");
  const [model, setModel] = useState(bike?.model ?? "");
  const [year, setYear] = useState(bike ? String(bike.year) : "");
  const [specs, setSpecs] = useState(bike?.specs ?? "");
  // Refetch in the hook-level onSuccess: a per-mutate() callback is skipped once
  // the form unmounts (Cancel or navigation mid-request), which would leave the list stale.
  const refetchTrip = { onSuccess: () => queryClient.invalidateQueries({ queryKey: getTripApiTripsSlugGetQueryKey({ slug }) }) };
  const create = useCreateBikeApiTripsSlugBikesPost({ mutation: refetchTrip });
  const patch = usePatchBikeApiTripsSlugBikesIdPatch({ mutation: refetchTrip });
  const mutation = bike ? patch : create;

  const yearNum = Number(year);
  const valid = riderName.trim() && make.trim() && model.trim() && year.trim() !== "" && Number.isInteger(yearNum);

  const onSuccess = () => onDone();

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid || mutation.isPending) return;
    const fields = { riderName: riderName.trim(), make: make.trim(), model: model.trim(), year: yearNum, specs };
    if (!bike) {
      create.mutate({ slug, data: { id, ...fields } }, { onSuccess });
      return;
    }
    // Only fields that differ from the bike the form opened with; never null.
    const data: BikePatch = {};
    for (const k of Object.keys(fields) as (keyof typeof fields)[]) {
      if (fields[k] !== bike[k]) Object.assign(data, { [k]: fields[k] });
    }
    if (Object.keys(data).length === 0) return onDone();
    patch.mutate({ slug, id: bike.id, data }, { onSuccess });
  }

  return (
    <form onSubmit={submit}>
      <label style={{ display: "block" }}>
        Rider name
        <input value={riderName} onChange={(e) => setRiderName(e.target.value)} required />
      </label>
      <label style={{ display: "block" }}>
        Make
        <input value={make} onChange={(e) => setMake(e.target.value)} required />
      </label>
      <label style={{ display: "block" }}>
        Model
        <input value={model} onChange={(e) => setModel(e.target.value)} required />
      </label>
      <label style={{ display: "block" }}>
        Year
        <input type="number" step={1} value={year} onChange={(e) => setYear(e.target.value)} required />
      </label>
      <label style={{ display: "block" }}>
        Specs
        <textarea value={specs} onChange={(e) => setSpecs(e.target.value)} />
      </label>
      {mutation.error && <p role="alert">{errorMessage(mutation.error)}</p>}
      <button type="submit" disabled={!valid || mutation.isPending}>
        {bike ? "Save bike" : "Add bike"}
      </button>
      {bike && (
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      )}
    </form>
  );
}
