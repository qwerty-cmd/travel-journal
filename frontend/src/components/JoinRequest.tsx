import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useCancelJoinRequestApiV2JoinRequestsRequestIdCancelPost } from "../api/gen/hooks/useCancelJoinRequestApiV2JoinRequestsRequestIdCancelPost";
import { useClaimTripApiV2TripsClaimPost } from "../api/gen/hooks/useClaimTripApiV2TripsClaimPost";
import { useCreateJoinRequestApiV2TripsTripIdJoinRequestsPost } from "../api/gen/hooks/useCreateJoinRequestApiV2TripsTripIdJoinRequestsPost";
import { listMyJoinRequestsApiV2MeJoinRequestsGetQueryKey, useListMyJoinRequestsApiV2MeJoinRequestsGet } from "../api/gen/hooks/useListMyJoinRequestsApiV2MeJoinRequestsGet";
import { getTripApiV2TripsTripIdGetQueryKey } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import type { ViewerRole } from "../api/gen/types/ViewerRole";
import type { MyJoinRequestOut } from "../api/gen/types/MyJoinRequestOut";
import { codePoints, errorText, formatRetry, isRateLimited, useMe, useRetryCountdown, useSingleFlight } from "../auth";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { StatusNotice } from "./StatusNotice";
import "./JoinRequest.css";

// Design feature: asking to join a trip (docs/design/screens/join-request.md and
// legacy-link.md; decision-log Entry 29). Render these only from `viewerRole()`;
// a record with no role gets nothing. A request never grants access: the server's
// `viewer.role` is the only thing that turns the form into Pending, and a leader
// decides the rest.
// Design format: JoinRequestPanel (v2 trip) = anonymous → "Sign in to ask to join"
// link carrying `next`; none → message textarea with a live "n / 280" counter,
// "Send request"; pending → PendingRequest. Refusals (409) show the envelope
// message verbatim and hide the form for this visit. LegacyJoinPanel (/t/$slug) =
// "Ask to join as a rider" (claim; the slug goes in the body only, never echoed).
// PendingRequest = Pending badge, notice, "Cancel request" (no confirm).
// APIs called: POST /api/v2/trips/{tripId}/join-requests, POST
// /api/v2/trips/claim, POST /api/v2/join-requests/{requestId}/cancel, GET
// /api/v2/me/join-requests (the request id for Cancel). After any write the v2 trip
// and the request list are invalidated; `onChanged` refetches a legacy trip.
export const MESSAGE_MAX = 280;
const OFFLINE = "You need a connection to send a request.";
const status = (e: unknown) => (e instanceof ApiError ? e.status : undefined);

function useRefresh(tripId: string, onChanged?: () => void) {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({ queryKey: getTripApiV2TripsTripIdGetQueryKey({ tripId }) });
    queryClient.invalidateQueries({ queryKey: listMyJoinRequestsApiV2MeJoinRequestsGetQueryKey() });
    onChanged?.();
  };
}

function ErrorNotice({ error, remaining }: { error: unknown; remaining: number }) {
  if (!error) return null;
  if (isRateLimited(error)) {
    return remaining > 0 ? <StatusNotice tone="warning" title="Too many requests." detail={formatRetry(remaining)} /> : null;
  }
  const refused = status(error) === 409;
  return <StatusNotice tone={refused ? "warning" : "danger"} alert title={errorText(error, OFFLINE)} />;
}

export function JoinRequestPanel({ tripId, role }: { tripId: string; role: ViewerRole | undefined }) {
  const refresh = useRefresh(tripId);
  const create = useCreateJoinRequestApiV2TripsTripIdJoinRequestsPost();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const [message, setMessage] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [created, setCreated] = useState<MyJoinRequestOut | null>(null);
  const [cancelled, setCancelled] = useState(false);

  if (role === "anonymous") {
    return (
      <section className="join" aria-label="Join this trip">
        <p className="join__muted">Sign in to ask the leaders to let you ride.</p>
        <Link to="/signin" search={{ next: `/trips/${tripId}` }} className="btn btn--secondary btn--md">
          Sign in to ask to join
        </Link>
      </section>
    );
  }
  if (role === "pending") return <PendingRequest tripId={tripId} fallback={created} onCancelled={() => setCancelled(true)} />;
  if (role !== "none") return null;

  const limited = countdown.remaining > 0;
  const refused = status(error) === 409;
  const invalid = status(error) === 422 ? errorText(error, "") : undefined;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (limited) return;
    flight.run((settle) => {
      setError(null);
      setCancelled(false);
      create.mutate(
        { tripId, data: { message: message.trim() || null } },
        {
          onSuccess: (request) => {
            setCreated(request);
            refresh();
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
            if (status(err) === 409) refresh();
          },
          onSettled: settle,
        },
      );
    });
  }

  return (
    <section className="join" aria-labelledby="join-heading">
      <h2 id="join-heading" className="join__h2">
        Ask to join this trip
      </h2>
      {cancelled ? <StatusNotice tone="success" title="Request cancelled" /> : null}
      {invalid ? null : <ErrorNotice error={error} remaining={countdown.remaining} />}
      {refused ? null : (
        <form className="join__form" noValidate onSubmit={onSubmit}>
          <MessageField value={message} onChange={setMessage} error={invalid} />
          <Button type="submit" loading={create.isPending} loadingLabel="Sending…" disabled={limited}>
            Send request
          </Button>
        </form>
      )}
    </section>
  );
}

function MessageField({ value, onChange, error }: { value: string; onChange: (v: string) => void; error?: string }) {
  const count = codePoints(value);
  return (
    <div className="field">
      <label className="field__label" htmlFor="join-message">
        Message to the leaders (optional)
      </label>
      <p className="field__helper">Say who you are, e.g. which bike you ride.</p>
      <textarea
        id="join-message"
        className={["field__input", "join__textarea", error && "field__input--error"].filter(Boolean).join(" ")}
        rows={3}
        value={value}
        aria-invalid={error ? true : undefined}
        aria-describedby="join-count"
        onChange={(e) => onChange(codePoints(e.target.value) > MESSAGE_MAX ? [...e.target.value].slice(0, MESSAGE_MAX).join("") : e.target.value)}
      />
      <p className="field__helper" id="join-count" aria-live="polite">
        {count} / {MESSAGE_MAX}
      </p>
      {error ? <p className="field__error">{error}</p> : null}
    </div>
  );
}

export function PendingRequest({ tripId, fallback, onCancelled, text }: { tripId: string; fallback?: MyJoinRequestOut | null; onCancelled?: () => void; text?: string }) {
  const refresh = useRefresh(tripId);
  const list = useListMyJoinRequestsApiV2MeJoinRequestsGet();
  const cancel = useCancelJoinRequestApiV2JoinRequestsRequestIdCancelPost();
  const flight = useSingleFlight();
  const [error, setError] = useState<unknown>(null);
  const request = list.data?.find((r) => r.tripId === tripId && r.state === "pending") ?? fallback ?? undefined;

  function onCancel() {
    if (!request) return;
    flight.run((settle) => {
      setError(null);
      cancel.mutate(
        { requestId: request.id },
        {
          onSuccess: () => {
            onCancelled?.();
            refresh();
          },
          onError: (err) => {
            setError(err);
            refresh();
          },
          onSettled: settle,
        },
      );
    });
  }

  return (
    <section className="join" aria-label="Your join request">
      <Badge variant="pending" />
      <StatusNotice tone="info" title={text ?? "Request sent. A leader of this trip will review it."} detail="You'll see Add stop once you're approved." />
      {request?.message ? <blockquote className="join__quote">{request.message}</blockquote> : null}
      {error ? <StatusNotice tone="info" alert title={errorText(error, OFFLINE)} /> : null}
      <Button variant="tertiary" loading={cancel.isPending} loadingLabel="Cancelling…" disabled={!request} onClick={onCancel}>
        Cancel request
      </Button>
    </section>
  );
}

/**
 * `/t/$slug`: the one-tap claim of an old rider link, for a signed-in non-member or pending
 * requester. `slug` is sent in the body and never rendered or logged. `/auth/me` is asked
 * only for roles that could show it, so members and role-less cached trips cost no request.
 */
export function LegacyJoinPanel(props: { slug: string; tripId: string; role: ViewerRole | undefined; onChanged: () => void }) {
  if (props.role !== "none" && props.role !== "pending") return null;
  return <SignedInLegacyJoin {...props} />;
}

function SignedInLegacyJoin({ slug, tripId, role, onChanged }: { slug: string; tripId: string; role: ViewerRole | undefined; onChanged: () => void }) {
  const signedIn = !!useMe().data;
  const refresh = useRefresh(tripId, onChanged);
  const claim = useClaimTripApiV2TripsClaimPost();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const [error, setError] = useState<unknown>(null);
  const [claimed, setClaimed] = useState<MyJoinRequestOut | null>(null);

  if (!signedIn) return null;
  if (role === "pending" || claimed) return <PendingRequest tripId={tripId} fallback={claimed} />;

  const notUsable = status(error) === 404;

  function onClaim() {
    if (countdown.remaining > 0) return;
    flight.run((settle) => {
      setError(null);
      claim.mutate(
        { data: { riderSlug: slug } },
        {
          onSuccess: (request) => {
            setClaimed(request);
            refresh();
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
            if (status(err) === 409) refresh();
          },
          onSettled: settle,
        },
      );
    });
  }

  return (
    <section className="join" aria-label="Join this trip">
      <p className="join__muted">To add stops, ask to join this trip.</p>
      {notUsable ? (
        <StatusNotice tone="warning" alert title="This link can't be used to join" detail="Ask a leader of this trip for the rider link, or to make the trip public." />
      ) : (
        <ErrorNotice error={error} remaining={countdown.remaining} />
      )}
      <Button variant="secondary" loading={claim.isPending} loadingLabel="Sending…" disabled={countdown.remaining > 0} onClick={onClaim}>
        Ask to join as a rider
      </Button>
    </section>
  );
}

