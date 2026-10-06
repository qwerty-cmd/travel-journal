import { useState } from "react";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { getTripApiV2TripsTripIdGetQueryKey, useGetTripApiV2TripsTripIdGet } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import { usePatchTripApiV2TripsTripIdPatch } from "../api/gen/hooks/usePatchTripApiV2TripsTripIdPatch";
import type { TripPatch } from "../api/gen/types/TripPatch";
import type { Visibility } from "../api/gen/types/Visibility";
import { codePoints, errorText, formatRetry, isOffline, isRateLimited, useRetryCountdown, useSingleFlight } from "../auth";
import { Button } from "../components/Button";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";
import { GlobeIcon, LockIcon } from "../icons";
import { saveTripById } from "../localStore";
import { viewerRole } from "../tripV2";
import "./trips.$tripId.settings.css";

// Design feature: leader-only trip settings at /trips/$tripId/settings
// (docs/design/screens/trip-settings.md; DESIGN.md §13 ruling X1).
// Design format: gated on viewerRole() alone; anyone else (or a cached trip with no
// role) gets "Leaders only" and no form. One form: name, visibility (radios; choosing
// public from private shows the Publish warning inline, as on create), delay 0-168
// with chips and a 0-hours warning. Save sends only the changed fields, never null;
// nothing changed sends nothing. 403 -> Forbidden notice, 422 -> envelope message,
// 429 -> message + Retry-After countdown; double taps send one request.
// APIs called: GET /api/v2/trips/{tripId} (shell cache, refetchOnMount: false);
// PATCH /api/v2/trips/{tripId} (200 TripOut -> cache + persisted copy + refetch).
export const Route = createFileRoute("/trips/$tripId/settings")({
  component: Settings,
});

const CHIPS = [0, 12, 24, 48];

function Settings() {
  const { tripId } = Route.useParams();
  const trip = useGetTripApiV2TripsTripIdGet({ tripId }, { query: { refetchOnMount: false } });
  if (!trip.data) return null;
  if (viewerRole(trip.data) !== "leader") {
    return (
      <section className="settings">
        <StatusNotice tone="info" title="Leaders only" detail="Only the leaders of this trip can change its settings." />
        <Link to="/trips/$tripId" params={{ tripId }} className="btn btn--secondary btn--md">
          Back to the trip
        </Link>
      </section>
    );
  }
  return <SettingsForm tripId={tripId} name={trip.data.name} visibility={trip.data.visibility} delay={trip.data.publicDelayHours} />;
}

function SettingsForm({ tripId, name: savedName, visibility: savedVis, delay: savedDelay }: { tripId: string; name: string; visibility: Visibility; delay: number }) {
  const queryClient = useQueryClient();
  const patch = usePatchTripApiV2TripsTripIdPatch();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const [name, setName] = useState(savedName);
  const [visibility, setVisibility] = useState<Visibility>(savedVis);
  const [delayText, setDelayText] = useState(String(savedDelay));
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);

  const trimmed = name.trim();
  const delay = /^\d+$/.test(delayText.trim()) ? Number(delayText.trim()) : NaN;
  const nameErr = !trimmed ? "Enter a trip name." : codePoints(trimmed) > 100 ? "Use 100 characters or fewer." : undefined;
  const delayErr = !(delay >= 0 && delay <= 168) ? "Choose 0 to 168 hours." : undefined;
  const limited = countdown.remaining > 0;
  const status = (error as { status?: number } | null)?.status;
  const serverMessage = status === 422 ? errorText(error, "") : undefined;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    setSaved(false);
    if (nameErr || delayErr || limited) return;
    const body: TripPatch = {};
    if (trimmed !== savedName) body.name = trimmed;
    if (visibility !== savedVis) body.visibility = visibility;
    if (delay !== savedDelay) body.publicDelayHours = delay;
    if (Object.keys(body).length === 0) return;
    flight.run((done) => {
      setError(null);
      patch.mutate(
        { tripId, data: body },
        {
          onSuccess: (updated) => {
            const key = getTripApiV2TripsTripIdGetQueryKey({ tripId });
            queryClient.setQueryData(key, updated);
            saveTripById(tripId, updated);
            void queryClient.invalidateQueries({ queryKey: key });
            setName(updated.name);
            setVisibility(updated.visibility);
            setDelayText(String(updated.publicDelayHours));
            setSaved(true);
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
          },
          onSettled: done,
        },
      );
    });
  }

  if (status === 403) {
    return (
      <section className="settings">
        <StatusNotice tone="danger" alert title="You're no longer a leader of this trip." detail="Only leaders can change its settings." />
        <Link to="/trips/$tripId" params={{ tripId }} className="btn btn--secondary btn--md">
          Back to the trip
        </Link>
      </section>
    );
  }

  return (
    <section className="settings">
      <h1>Trip settings</h1>
      {isOffline(error) ? (
        <StatusNotice tone="danger" alert title="Changing settings needs a connection." />
      ) : isRateLimited(error) ? (
        limited ? <StatusNotice tone="warning" title={errorText(error, "")} detail={formatRetry(countdown.remaining)} /> : null
      ) : error && status !== 422 ? (
        <StatusNotice tone="danger" alert title={errorText(error, "")} />
      ) : null}
      {saved ? <StatusNotice tone="success" title="Saved." /> : null}
      <form className="settings__form" noValidate onSubmit={onSubmit}>
        <TextField label="Trip name" name="name" autoComplete="off" value={name} onChange={(e) => setName(e.target.value)} error={tried ? nameErr : undefined} />
        <fieldset className="settings__group">
          <legend className="settings__legend">Who can see this trip?</legend>
          <label className="settings__option">
            <input type="radio" name="visibility" value="public" checked={visibility === "public"} onChange={() => setVisibility("public")} />
            <span>
              <GlobeIcon /> Public: anyone can find and follow it, and riders can ask to join.
            </span>
          </label>
          <label className="settings__option">
            <input type="radio" name="visibility" value="private" checked={visibility === "private"} onChange={() => setVisibility("private")} />
            <span>
              <LockIcon /> Private: only members can see it. Nobody new can ask to join.
            </span>
          </label>
        </fieldset>
        {savedVis === "private" && visibility === "public" ? (
          <StatusNotice
            tone="warning"
            title="Check your first stop."
            detail="Trips often start at someone's home. Public viewers see each stop's exact location. Publishing shares everything already added, not only new stops."
          />
        ) : null}
        <TextField
          label="Hours before stops are public"
          name="publicDelayHours"
          inputMode="numeric"
          helper="A delay stops the public seeing where you are right now, or where you're camping tonight. 0 means stops are public immediately."
          value={delayText}
          onChange={(e) => setDelayText(e.target.value)}
          error={delayErr && (tried || delayText !== String(savedDelay)) ? delayErr : undefined}
        />
        <div className="settings__chips">
          {CHIPS.map((c) => (
            <Button key={c} variant="secondary" onClick={() => setDelayText(String(c))}>
              {c}
            </Button>
          ))}
        </div>
        {delay === 0 ? (
          <StatusNotice tone="warning" title="With no delay, anyone can see your latest location as soon as you add a stop." />
        ) : null}
        {serverMessage ? <StatusNotice tone="danger" alert title={serverMessage} /> : null}
        <Button type="submit" size="lg" loading={patch.isPending} loadingLabel="Saving…" disabled={limited || !navigator.onLine}>
          Save settings
        </Button>
      </form>
    </section>
  );
}
