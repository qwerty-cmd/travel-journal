import { useEffect, useRef, useState } from "react";
import { Navigate, createFileRoute, useRouter } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useCreateTripApiV2TripsPost } from "../api/gen/hooks/useCreateTripApiV2TripsPost";
import { getTripApiV2TripsTripIdGetQueryKey } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import type { Visibility } from "../api/gen/types/Visibility";
import { codePoints, errorText, formatRetry, isOffline, isRateLimited, useMe, useRetryCountdown, useSingleFlight, useSlow } from "../auth";
import { AuthPage } from "../components/AuthPage";
import { Button } from "../components/Button";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";
import { GlobeIcon, LockIcon } from "../icons";
import { saveTripById } from "../localStore";
import "./trips.new.css";

// Design feature: `/trips/new` (docs/design/screens/create-trip.md; decision-log
// Entry 29). A signed-in person starts a trip and becomes its first leader. Online
// only, never queued; the client id makes a retry an idempotent replay.
// Design format: AuthPage "New trip" → H1 → Trip name → Start date (native) →
// "Who can see it?" fieldset of two radio cards (Public default) with the matching
// inline notice → "Create trip" ("Creating…"). Choosing public shows the Publish warning inline (not a dialog). 429 shows the
// envelope message + Retry-After countdown and disables Create; 409 shows the
// message verbatim and the next tap uses a fresh id; offline shows a danger notice
// and disables Create. Signed out → /signin?next=/trips/new.
// APIs called: POST /api/v2/trips (201 new / 200 replay → /trips/$tripId; the
// returned TripOut is cached for it), GET /api/v2/auth/me (useMe).
export const Route = createFileRoute("/trips/new")({
  component: NewTrip,
});

const OFFLINE = "You need a connection to create a trip.";

function useOnline(): boolean {
  const [online, setOnline] = useState(() => navigator.onLine);
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

function NewTrip() {
  const me = useMe();
  const router = useRouter();
  const queryClient = useQueryClient();
  const create = useCreateTripApiV2TripsPost();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const slow = useSlow(create.isPending);
  const online = useOnline();
  const summary = useRef<HTMLDivElement>(null);
  // The client id of the attempt in progress: kept across retries so a replay returns
  // the existing trip; dropped on success and on a 409 (the id is not ours).
  const attemptId = useRef<string | null>(null);

  const [name, setName] = useState("");
  const [startDate, setStartDate] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("public");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (me.error?.status === 401) return <Navigate to="/signin" search={{ next: "/trips/new" }} replace />;

  const trimmed = name.trim();
  const nameErr = !tried ? undefined : !trimmed ? "Enter a trip name." : codePoints(trimmed) > 100 ? "Use 100 characters or fewer." : undefined;
  const dateErr = tried && !startDate ? "Choose a start date." : undefined;
  const limited = countdown.remaining > 0;
  const offline = !online;

  function send() {
    flight.run((done) => {
      setError(null);
      attemptId.current ??= crypto.randomUUID();
      create.mutate(
        { data: { id: attemptId.current, name: trimmed, startDate, visibility } },
        {
          onSuccess: (trip) => {
            attemptId.current = null;
            queryClient.setQueryData(getTripApiV2TripsTripIdGetQueryKey({ tripId: trip.id }), trip);
            saveTripById(trip.id, trip);
            router.navigate({ to: "/trips/$tripId", params: { tripId: trip.id } });
          },
          onError: (err) => {
            if (err.status === 409) attemptId.current = null;
            setError(err);
            countdown.startFrom(err);
            setTimeout(() => summary.current?.focus());
          },
          onSettled: done,
        },
      );
    });
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    if (!trimmed || codePoints(trimmed) > 100 || !startDate || limited || offline) return;
    send();
  }

  return (
    <AuthPage title="New trip">
      <h1>Create a trip</h1>
      {offline || isOffline(error) ? (
        <StatusNotice ref={summary} tone="danger" alert title={OFFLINE} />
      ) : isRateLimited(error) ? (
        limited ? <StatusNotice ref={summary} tone="warning" title={errorText(error, "")} detail={formatRetry(countdown.remaining)} /> : null
      ) : error ? (
        <StatusNotice ref={summary} tone={(error as { status?: number }).status === 409 ? "warning" : "danger"} alert title={errorText(error, OFFLINE)} />
      ) : null}
      {slow ? <StatusNotice tone="info" title="Waking up the server…" detail="The first visit after a quiet spell can take a little while." /> : null}
      <form className="create-trip__form" noValidate onSubmit={onSubmit}>
        <TextField
          label="Trip name"
          name="name"
          helper="For example: Spring loop 2026"
          autoComplete="off"
          value={name}
          onChange={(e) => setName(e.target.value)}
          error={nameErr}
        />
        <TextField
          className="create-trip__date"
          label="Start date"
          name="startDate"
          type="date"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
          error={dateErr}
        />
        <fieldset className="create-trip__visibility">
          <legend className="create-trip__legend">Who can see it?</legend>
          <label className={`create-trip__option${visibility === "public" ? " create-trip__option--selected" : ""}`}>
            <input type="radio" name="visibility" value="public" checked={visibility === "public"} onChange={() => setVisibility("public")} />
            <span>
              <span className="create-trip__option-title">
                <GlobeIcon /> Public
              </span>
              <p className="create-trip__option-text">Anyone can find it and follow along. Stops appear publicly 24 hours after they're added.</p>
            </span>
          </label>
          <label className={`create-trip__option${visibility === "private" ? " create-trip__option--selected" : ""}`}>
            <input type="radio" name="visibility" value="private" checked={visibility === "private"} onChange={() => setVisibility("private")} />
            <span>
              <span className="create-trip__option-title">
                <LockIcon /> Private
              </span>
              <p className="create-trip__option-text">Only members can see it. Nobody can ask to join a private trip.</p>
            </span>
          </label>
        </fieldset>
        {visibility === "public" ? (
          <StatusNotice tone="warning" title="Check your first stop." detail="Trips often start at someone's home. Public viewers see each stop's exact location." />
        ) : (
          <StatusNotice
            tone="info"
            title="Riders can't join a private trip."
            detail="If others are riding with you, keep it public until they've joined. You can make it private later in Trip settings, and members keep their access."
          />
        )}
        <Button type="submit" size="lg" loading={create.isPending} loadingLabel="Creating…" disabled={limited || offline}>
          Create trip
        </Button>
      </form>
    </AuthPage>
  );
}
