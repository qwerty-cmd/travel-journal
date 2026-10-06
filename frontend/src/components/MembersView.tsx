import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { getTripApiV2TripsTripIdGetQueryKey } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import { listMembersApiV2TripsTripIdMembersGetQueryKey, useListMembersApiV2TripsTripIdMembersGet } from "../api/gen/hooks/useListMembersApiV2TripsTripIdMembersGet";
import { usePromoteMemberApiV2TripsTripIdMembersUserIdPromotePost } from "../api/gen/hooks/usePromoteMemberApiV2TripsTripIdMembersUserIdPromotePost";
import { useRevokeMemberApiV2TripsTripIdMembersUserIdDelete } from "../api/gen/hooks/useRevokeMemberApiV2TripsTripIdMembersUserIdDelete";
import { listMyTripsApiV2MeTripsGetQueryKey } from "../api/gen/hooks/useListMyTripsApiV2MeTripsGet";
import { useStepDownApiV2TripsTripIdStepDownPost } from "../api/gen/hooks/useStepDownApiV2TripsTripIdStepDownPost";
import { useLeaveTripApiV2TripsTripIdLeavePost } from "../api/gen/hooks/useLeaveTripApiV2TripsTripIdLeavePost";
import type { MemberOut } from "../api/gen/types/MemberOut";
import { errorText, formatRetry, isOffline, isRateLimited, useMe, useRetryCountdown, useSingleFlight } from "../auth";
import { formatDate } from "../format";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { Dialog, DialogActions } from "./Dialog";
import { StatusNotice } from "./StatusNotice";
import "./LeaderReview.css";

// Design feature: members management (docs/design/screens/members.md; DESIGN.md §13 X1).
// Mounted for a leader or a rider (the route gates on `viewerRole()`; the server enforces).
// Design format: Leaders list then Riders list. Leader view: "Make leader" and "Revoke"
// (each behind a confirm dialog) on rider rows; Revoke is never offered on a leader row or
// on your own row; other leaders' rows say they can't be removed. Your own row: "Step down"
// (leaders) and "Leave trip" (everyone), both confirmed; both disabled with the contract's
// last-leader message when you are the only leader. A rider sees the list read-only.
// One write at a time (a ref, so a double tap sends one request). After any change the
// members list and the trip are refetched; a leave also refetches /me/trips and goes home (a private trip would 404 for the leaver).
// APIs called: GET /api/v2/trips/{tripId}/members, POST .../members/{userId}/promote,
// DELETE .../members/{userId}, POST .../step-down, POST .../leave.
const OFFLINE = "Managing members needs a connection.";
const SOLE_LEADER = "You're the only leader. Promote another rider to leader first.";
const status = (e: unknown) => (e instanceof ApiError ? e.status : undefined);

type Confirm = { kind: "promote" | "revoke"; m: MemberOut } | { kind: "stepdown" | "leave" };

export function MembersView({ tripId, role, tripName }: { tripId: string; role: "leader" | "rider"; tripName: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const me = useMe();
  const list = useListMembersApiV2TripsTripIdMembersGet({ tripId });
  const promote = usePromoteMemberApiV2TripsTripIdMembersUserIdPromotePost();
  const revoke = useRevokeMemberApiV2TripsTripIdMembersUserIdDelete();
  const stepDown = useStepDownApiV2TripsTripIdStepDownPost();
  const leave = useLeaveTripApiV2TripsTripIdLeavePost();
  const countdown = useRetryCountdown();
  const flight = useSingleFlight();
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);

  const isLeader = role === "leader";
  const myId = me.data?.id;
  const offline = isOffline(list.error);
  const blocked = offline || countdown.remaining > 0 || busy;
  const all = list.data ?? [];
  const leaders = all.filter((m) => m.role === "leader");
  const riders = all.filter((m) => m.role !== "leader");
  const soleLeader = isLeader && leaders.length === 1;

  function refresh() {
    queryClient.invalidateQueries({ queryKey: listMembersApiV2TripsTripIdMembersGetQueryKey({ tripId }) });
    queryClient.invalidateQueries({ queryKey: getTripApiV2TripsTripIdGetQueryKey({ tripId }) });
  }

  function run(send: () => Promise<unknown>, onOk: () => void) {
    flight.run((settle) => {
      setFailure(null);
      setMessage(null);
      setDialogError(null);
      setBusy(true);
      void (async () => {
        try {
          await send();
          setConfirm(null);
          onOk();
          refresh();
        } catch (error) {
          countdown.startFrom(error);
          const s = status(error);
          if (s === 409) setDialogError(errorText(error, OFFLINE));
          else {
            setConfirm(null);
            setFailure(error);
            if (s === 403 || s === 404) refresh();
          }
        } finally {
          setBusy(false);
          settle();
        }
      })();
    });
  }

  function onConfirm() {
    if (!confirm) return;
    if (confirm.kind === "promote") {
      const m = confirm.m;
      run(() => promote.mutateAsync({ tripId, userId: m.userId }), () => setMessage(`${m.displayName} is now a leader`));
    } else if (confirm.kind === "revoke") {
      const m = confirm.m;
      run(() => revoke.mutateAsync({ tripId, userId: m.userId }), () => setMessage(`Removed ${m.displayName}`));
    } else if (confirm.kind === "stepdown") {
      run(() => stepDown.mutateAsync({ tripId }), () => setMessage("You're now a rider on this trip"));
    } else {
      run(
        () => leave.mutateAsync({ tripId }),
        () => {
          queryClient.invalidateQueries({ queryKey: listMyTripsApiV2MeTripsGetQueryKey() });
          void navigate({ to: "/" });
        },
      );
    }
  }

  const close = () => (busy ? undefined : setConfirm(null));
  const row = (m: MemberOut) => {
    const you = m.userId === myId;
    const name = m.displayName;
    return (
      <li key={m.userId} className="review__card">
        <div className="review__who">
          <p className="review__name">
            {name}
            {you ? " (You)" : ""}
          </p>
          <Badge variant={m.role === "leader" ? "leader" : "rider"} />
        </div>
        <p className="review__muted">Joined {formatDate(m.joinedAt.slice(0, 10))}</p>
        {m.role === "leader" && !you ? <p className="review__muted">Leaders can't remove each other.</p> : null}
        <div className="review__actions">
          {isLeader && m.role !== "leader" ? (
            <>
              <Button variant="secondary" disabled={blocked} onClick={() => setConfirm({ kind: "promote", m })} aria-label={`Make ${name} a leader`}>
                Make leader
              </Button>
              <Button variant="danger-secondary" disabled={blocked} onClick={() => setConfirm({ kind: "revoke", m })} aria-label={`Revoke ${name}`}>
                Revoke
              </Button>
            </>
          ) : null}
          {you && m.role === "leader" ? (
            <Button variant="danger-secondary" disabled={blocked || soleLeader} onClick={() => setConfirm({ kind: "stepdown" })}>
              Step down
            </Button>
          ) : null}
          {you ? (
            <Button variant="tertiary" disabled={blocked || soleLeader} onClick={() => setConfirm({ kind: "leave" })}>
              Leave trip
            </Button>
          ) : null}
        </div>
        {you && soleLeader ? <p className="review__muted">{SOLE_LEADER}</p> : null}
      </li>
    );
  };

  const target = confirm && "m" in confirm ? confirm.m.displayName : "";
  return (
    <section className="review" aria-labelledby="members-heading">
      {isLeader ? (
        <nav className="review__tabs" aria-label="Leader area">
          <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "requests" }} className="review__tab">
            Requests
          </Link>
          <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "members" }} className="review__tab review__tab--active" aria-current="page">
            Members
          </Link>
          <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "blocked" }} className="review__tab">
            Blocked
          </Link>
        </nav>
      ) : null}
      <h2 id="members-heading" className="review__h2">
        Members
      </h2>
      <p className="review__muted">Leaders can approve requests and manage riders. Every leader has the same powers.</p>

      <div aria-live="polite" className="review__live">
        {message ? <StatusNotice tone="success" title={message} /> : null}
      </div>
      {isRateLimited(failure) ? (
        <StatusNotice tone="warning" alert title="Too many requests." detail={countdown.remaining > 0 ? formatRetry(countdown.remaining) : undefined} />
      ) : status(failure) === 401 ? (
        <div className="review__notice">
          <StatusNotice tone="danger" alert title="Your session has ended. Sign in to continue." />
          <Link to="/signin" search={{ next: `/trips/${tripId}/members?view=members` }} className="btn btn--secondary btn--md">
            Sign in
          </Link>
        </div>
      ) : failure ? (
        <StatusNotice tone="danger" alert title={errorText(failure, OFFLINE)} />
      ) : null}
      {offline ? <StatusNotice tone="offline" title={OFFLINE} /> : null}

      {list.isPending && !list.isError ? (
        <div className="review__list" aria-busy="true">
          <span className="visually-hidden">Loading…</span>
          {[0, 1, 2].map((i) => (
            <div key={i} className="review__skeleton" />
          ))}
        </div>
      ) : list.isError ? (
        offline ? null : (
          <>
            <StatusNotice tone="danger" alert title={errorText(list.error, OFFLINE)} />
            <Button variant="secondary" onClick={() => list.refetch()}>
              Try again
            </Button>
          </>
        )
      ) : (
        <>
          <h3 className="review__h2">Leaders ({leaders.length})</h3>
          <ul className="review__list">{leaders.map(row)}</ul>
          <h3 className="review__h2">Riders ({riders.length})</h3>
          {riders.length === 0 ? (
            <p className="review__empty">No riders yet. Approve requests to add riders.</p>
          ) : (
            <ul className="review__list">{riders.map(row)}</ul>
          )}
          {isLeader ? (
            <p className="review__muted">Removed riders can ask to join again after 7 days. To stop that, block them from the Requests list when they ask.</p>
          ) : null}
        </>
      )}

      <Dialog open={confirm !== null} onClose={close} titleId="members-confirm-title">
        {confirm ? (
          <>
            <h2 id="members-confirm-title" className="review__h2">
              {confirm.kind === "promote"
                ? `Make ${target} a leader?`
                : confirm.kind === "revoke"
                  ? `Remove ${target}?`
                  : confirm.kind === "stepdown"
                    ? "Step down as leader?"
                    : `Leave ${tripName}?`}
            </h2>
            <p>
              {confirm.kind === "promote"
                ? `${target} will be able to approve requests, manage riders and change the trip's settings. Leaders can't remove each other, so ${target} can only stop being a leader by stepping down.`
                : confirm.kind === "revoke"
                  ? `${target} will lose access to this trip, and anything they have queued but not yet uploaded will be refused. They can ask to join again after 7 days.`
                  : confirm.kind === "stepdown"
                    ? "You'll become a rider. You won't be able to approve requests or manage riders until another leader makes you a leader again."
                    : "You'll lose access to this trip and anything you have queued but not yet uploaded will be refused. Leaving doesn't start the 7-day wait to ask again."}
            </p>
            {dialogError ? <StatusNotice tone="danger" alert title={dialogError} /> : null}
            <DialogActions>
              <Button variant={confirm.kind === "promote" ? "primary" : "danger"} loading={busy} loadingLabel="Working…" onClick={onConfirm}>
                {confirm.kind === "promote" ? "Make leader" : confirm.kind === "revoke" ? `Remove ${target}` : confirm.kind === "stepdown" ? "Step down" : "Leave trip"}
              </Button>
              <Button variant="secondary" autoFocus disabled={busy} onClick={close}>
                Cancel
              </Button>
            </DialogActions>
          </>
        ) : null}
      </Dialog>
    </section>
  );
}
