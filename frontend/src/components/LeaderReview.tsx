import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { getTripApiV2TripsTripIdGetQueryKey } from "../api/gen/hooks/useGetTripApiV2TripsTripIdGet";
import { listTripJoinRequestsApiV2TripsTripIdJoinRequestsGetQueryKey, useListTripJoinRequestsApiV2TripsTripIdJoinRequestsGet } from "../api/gen/hooks/useListTripJoinRequestsApiV2TripsTripIdJoinRequestsGet";
import { useDecideJoinRequestApiV2TripsTripIdJoinRequestsRequestIdDecisionPost } from "../api/gen/hooks/useDecideJoinRequestApiV2TripsTripIdJoinRequestsRequestIdDecisionPost";
import { useUnblockJoinRequestApiV2TripsTripIdJoinRequestsRequestIdUnblockPost } from "../api/gen/hooks/useUnblockJoinRequestApiV2TripsTripIdJoinRequestsRequestIdUnblockPost";
import type { JoinDecisionAction } from "../api/gen/types/JoinDecisionAction";
import type { TripJoinRequestOut } from "../api/gen/types/TripJoinRequestOut";
import { errorText, formatRetry, isOffline, isRateLimited, useRetryCountdown, useSingleFlight } from "../auth";
import { formatInstant, formatRelative } from "../format";
import { Badge } from "./Badge";
import { Button } from "./Button";
import { Dialog, DialogActions } from "./Dialog";
import { StatusNotice } from "./StatusNotice";
import "./LeaderReview.css";

// Design feature: the leader's join-request review (docs/design/screens/leader-review.md;
// decision-log Entry 29). Mounted only for a leader (the route gates on `viewerRole()`;
// the server enforces), so nothing is loaded for anyone else.
// Design format: Requests / Blocked segmented control; Requests = oldest first, each
// row a checkbox, display name, "Requested <relative>", quoted message, "Via old rider
// link" badge, Approve / Reject / "More options" -> "Reject and block" (confirm dialog).
// Bulk bar for >= 1 selected: one decision request per row, in sequence, each outcome
// reported; failed rows stay selected; a 429 or 401/403 stops the loop. 409/404 on a
// row = "Already handled". Blocked = Unblock (no confirm). Display names only: the
// requester's user id is a React key and a path segment, never rendered.
// APIs called: GET /api/v2/trips/{tripId}/join-requests (state=pending|blocked), POST
// .../join-requests/{requestId}/decision, POST .../join-requests/{requestId}/unblock.
// After any write the list and the trip (riderCount) are invalidated.
export type ReviewView = "requests" | "blocked";
const OFFLINE = "Reviewing requests needs a connection.";
const PENDING_CAP = 100;
const status = (e: unknown) => (e instanceof ApiError ? e.status : undefined);

type Failure = { error: unknown; summary?: string };
type Done = { ok: true } | { ok: false; error: unknown };
const PAST: Record<JoinDecisionAction, string> = { approve: "Approved", reject: "Rejected", reject_and_block: "Blocked" };
const VERB: Record<JoinDecisionAction, string> = { approve: "approve", reject: "reject", reject_and_block: "block" };

export function LeaderReview({ tripId, view }: { tripId: string; view: ReviewView }) {
  const queryClient = useQueryClient();
  const state = view === "blocked" ? "blocked" : "pending";
  const list = useListTripJoinRequestsApiV2TripsTripIdJoinRequestsGet({ tripId, params: { state } });
  const decide = useDecideJoinRequestApiV2TripsTripIdJoinRequestsRequestIdDecisionPost();
  const unblock = useUnblockJoinRequestApiV2TripsTripIdJoinRequestsRequestIdUnblockPost();
  const countdown = useRetryCountdown();
  const bulkFlight = useSingleFlight();
  const inflight = useRef(new Set<string>());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [gone, setGone] = useState<Set<string>>(new Set());
  const [handled, setHandled] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [blockTarget, setBlockTarget] = useState<TripJoinRequestOut | null>(null);
  const approveRefs = useRef(new Map<string, HTMLButtonElement>());
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [focusId, setFocusId] = useState<string | "heading" | null>(null);

  useEffect(() => {
    if (focusId === null) return;
    (focusId === "heading" ? headingRef.current : approveRefs.current.get(focusId))?.focus();
    setFocusId(null);
  }, [focusId]);

  function refresh() {
    queryClient.invalidateQueries({ queryKey: listTripJoinRequestsApiV2TripsTripIdJoinRequestsGetQueryKey({ tripId }) });
    queryClient.invalidateQueries({ queryKey: getTripApiV2TripsTripIdGetQueryKey({ tripId }) });
  }

  const rows = (list.data ?? []).filter((r) => !gone.has(r.id));
  const offline = isOffline(list.error);
  const limited = countdown.remaining > 0;
  const blocked = offline || limited;

  /** One write, deduped per request id (a ref: two taps can land before a re-render). */
  async function write(r: TripJoinRequestOut, send: () => Promise<unknown>): Promise<Done | null> {
    if (inflight.current.has(r.id)) return null;
    inflight.current.add(r.id);
    setBusy(new Set(inflight.current));
    try {
      await send();
      return { ok: true };
    } catch (error) {
      countdown.startFrom(error);
      if (status(error) === 409 || status(error) === 404) {
        setHandled((h) => ({ ...h, [r.id]: errorText(error, OFFLINE) }));
        refresh();
      }
      return { ok: false, error };
    } finally {
      inflight.current.delete(r.id);
      setBusy(new Set(inflight.current));
    }
  }

  const send = (r: TripJoinRequestOut, action: JoinDecisionAction) =>
    write(r, () => decide.mutateAsync({ tripId, requestId: r.id, data: { action } }));

  function collapse(r: TripJoinRequestOut) {
    const i = rows.findIndex((x) => x.id === r.id);
    const next = rows.slice(i + 1).find((x) => !handled[x.id]) ?? rows.slice(0, i).reverse().find((x) => !handled[x.id]);
    setGone((g) => new Set(g).add(r.id));
    setSelected((s) => {
      const n = new Set(s);
      n.delete(r.id);
      return n;
    });
    setFocusId(next && view === "requests" ? next.id : "heading");
  }

  async function act(r: TripJoinRequestOut, action: JoinDecisionAction) {
    setFailure(null);
    setMessage(null);
    const done = await send(r, action);
    if (!done) return;
    if (done.ok) {
      setMessage(`${PAST[action]} ${r.requester.displayName}`);
      collapse(r);
      refresh();
    } else if (status(done.error) !== 409 && status(done.error) !== 404) setFailure({ error: done.error });
  }

  async function onUnblock(r: TripJoinRequestOut) {
    setFailure(null);
    setMessage(null);
    const done = await write(r, () => unblock.mutateAsync({ tripId, requestId: r.id }));
    if (!done) return;
    if (done.ok) {
      setMessage(`Unblocked ${r.requester.displayName}. They can ask to join again once 7 days have passed since the original decision.`);
      collapse(r);
      refresh();
    } else if (status(done.error) !== 409 && status(done.error) !== 404) setFailure({ error: done.error });
  }

  function onBulk(action: "approve" | "reject") {
    const targets = rows.filter((r) => selected.has(r.id) && !handled[r.id]);
    if (targets.length === 0) return;
    bulkFlight.run((settle) => {
      setFailure(null);
      setMessage(null);
      setBulkBusy(true);
      void (async () => {
        let ok = 0;
        let first: { name: string; error: unknown } | null = null;
        let stopped: unknown = null;
        for (const r of targets) {
          const done = await send(r, action);
          if (!done) continue;
          if (done.ok) {
            ok += 1;
            setGone((g) => new Set(g).add(r.id));
            setSelected((s) => {
              const n = new Set(s);
              n.delete(r.id);
              return n;
            });
          } else {
            first ??= { name: r.requester.displayName, error: done.error };
            if (isRateLimited(done.error) || status(done.error) === 401 || status(done.error) === 403) {
              stopped = done.error;
              break;
            }
          }
        }
        if (first) {
          setFailure({ summary: `${PAST[action]} ${ok} of ${targets.length}. Couldn't ${VERB[action]} ${first.name}: ${errorText(first.error, OFFLINE)}`, error: stopped ?? first.error });
        } else setMessage(`${PAST[action]} ${ok}`);
        setBulkBusy(false);
        refresh();
        setFocusId("heading");
        settle();
      })();
    });
  }

  const toggle = (id: string) =>
    setSelected((s) => {
      const n = new Set(s);
      if (!n.delete(id)) n.add(id);
      return n;
    });
  const selectable = rows.filter((r) => !handled[r.id]);
  const allSelected = selectable.length > 0 && selectable.every((r) => selected.has(r.id));

  return (
    <section className="review" aria-labelledby="review-heading">
      <nav className="review__tabs" aria-label="Leader area">
        <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "requests" }} className={`review__tab${view === "requests" ? " review__tab--active" : ""}`} aria-current={view === "requests" ? "page" : undefined}>
          Requests{view === "requests" && list.data && list.data.length > 0 ? ` ${list.data.length}` : ""}
        </Link>
        <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "members" }} className="review__tab">
          Members
        </Link>
        <Link to="/trips/$tripId/members" params={{ tripId }} search={{ view: "blocked" }} className={`review__tab${view === "blocked" ? " review__tab--active" : ""}`} aria-current={view === "blocked" ? "page" : undefined}>
          Blocked
        </Link>
      </nav>
      <h2 id="review-heading" className="review__h2" tabIndex={-1} ref={headingRef}>
        {view === "blocked" ? "Blocked" : "Requests"}
      </h2>
      {view === "requests" ? <p className="review__muted">Approve people you know are riding. Approved riders can add stops and photos, and see the trip live.</p> : null}

      <div aria-live="polite" className="review__live">
        {message ? <StatusNotice tone="success" title={message} /> : null}
      </div>
      <FailureNotice failure={failure} remaining={countdown.remaining} tripId={tripId} />
      {offline ? <StatusNotice tone="offline" title={OFFLINE} /> : null}
      {view === "requests" && list.data && list.data.length >= PENDING_CAP ? (
        <StatusNotice tone="info" title="This trip has 100 requests waiting. New requests are refused until you review some." />
      ) : null}

      {list.isPending && !list.isError ? (
        <div className="review__list" aria-busy="true">
          <span className="visually-hidden">Loading…</span>
          {[0, 1, 2].map((i) => (
            <div key={i} className="review__skeleton" />
          ))}
        </div>
      ) : list.isError ? (
        offline ? null : status(list.error) === 401 || status(list.error) === 403 ? (
          <FailureNotice failure={{ error: list.error }} remaining={0} tripId={tripId} />
        ) : (
          <>
            <StatusNotice tone="danger" alert title={errorText(list.error, OFFLINE)} />
            <Button variant="secondary" onClick={() => list.refetch()}>
              Try again
            </Button>
          </>
        )
      ) : rows.length === 0 ? (
        <p className="review__empty">{view === "blocked" ? "Nobody is blocked." : "No requests right now. When someone asks to join, they'll appear here."}</p>
      ) : (
        <>
          {view === "requests" && rows.length >= 2 ? (
            <label className="review__all">
              <input type="checkbox" checked={allSelected} onChange={() => setSelected(allSelected ? new Set() : new Set(selectable.map((r) => r.id)))} />
              Select all
            </label>
          ) : null}
          {view === "requests" && selected.size > 0 ? (
            <div className="review__bulk" role="group" aria-label="Bulk actions">
              <span>{selected.size} selected</span>
              <Button loading={bulkBusy} loadingLabel="Working…" disabled={blocked} onClick={() => onBulk("approve")}>
                Approve {selected.size}
              </Button>
              <Button variant="secondary" disabled={blocked || bulkBusy} onClick={() => onBulk("reject")}>
                Reject {selected.size}
              </Button>
              <Button variant="tertiary" disabled={bulkBusy} onClick={() => setSelected(new Set())}>
                Clear
              </Button>
            </div>
          ) : null}
          <ul className="review__list">
            {rows.map((r) => (
              <Row
                key={r.id}
                r={r}
                view={view}
                busy={busy.has(r.id)}
                aria-busy={bulkBusy && selected.has(r.id)}
                handled={handled[r.id]}
                selected={selected.has(r.id)}
                disabled={blocked || bulkBusy}
                onToggle={() => toggle(r.id)}
                onAct={(a) => act(r, a)}
                onBlock={() => setBlockTarget(r)}
                onUnblock={() => onUnblock(r)}
                approveRef={(el) => {
                  if (el) approveRefs.current.set(r.id, el);
                  else approveRefs.current.delete(r.id);
                }}
              />
            ))}
          </ul>
        </>
      )}

      <Dialog open={blockTarget !== null} onClose={() => setBlockTarget(null)} titleId="block-title">
        {blockTarget ? (
          <>
            <h2 id="block-title" className="review__h2">
              Reject and block {blockTarget.requester.displayName}?
            </h2>
            <p>They won't be able to ask to join this trip again unless you unblock them.</p>
            <DialogActions>
              <Button
                variant="danger"
                onClick={() => {
                  const r = blockTarget;
                  setBlockTarget(null);
                  void act(r, "reject_and_block");
                }}
              >
                Reject and block
              </Button>
              <Button variant="secondary" onClick={() => setBlockTarget(null)}>
                Cancel
              </Button>
            </DialogActions>
          </>
        ) : null}
      </Dialog>
    </section>
  );
}

function FailureNotice({ failure, remaining, tripId }: { failure: Failure | null; remaining: number; tripId: string }) {
  if (!failure) return null;
  const { error, summary } = failure;
  if (isRateLimited(error)) {
    return <StatusNotice tone="warning" alert title="Too many requests." detail={remaining > 0 ? formatRetry(remaining) : summary} />;
  }
  if (status(error) === 401) {
    return (
      <div className="review__notice">
        <StatusNotice tone="danger" alert title="Your session has ended. Sign in to continue." />
        <Link to="/signin" search={{ next: `/trips/${tripId}/members` }} className="btn btn--secondary btn--md">
          Sign in
        </Link>
      </div>
    );
  }
  if (status(error) === 403) {
    return <StatusNotice tone="danger" alert title={error instanceof ApiError && error.envelope ? error.envelope.error.message : "You're no longer a leader on this trip."} />;
  }
  return <StatusNotice tone="danger" alert title={summary ?? errorText(error, OFFLINE)} />;
}

interface RowProps {
  r: TripJoinRequestOut;
  view: ReviewView;
  busy: boolean;
  "aria-busy": boolean;
  handled?: string;
  selected: boolean;
  disabled: boolean;
  onToggle: () => void;
  onAct: (a: JoinDecisionAction) => void;
  onBlock: () => void;
  onUnblock: () => void;
  approveRef: (el: HTMLButtonElement | null) => void;
}

function Row({ r, view, busy, handled, selected, disabled, onToggle, onAct, onBlock, onUnblock, approveRef, ...rest }: RowProps) {
  const [more, setMore] = useState(false);
  const name = r.requester.displayName;
  return (
    <li className="review__card" aria-busy={rest["aria-busy"] || undefined}>
      <div className="review__who">
        {view === "requests" && !handled ? <input type="checkbox" aria-label={`Select ${name}`} checked={selected} onChange={onToggle} /> : null}
        <div>
          <p className="review__name">{name}</p>
          <p className="review__muted" title={formatInstant(r.createdAt)}>
            Requested {formatRelative(r.createdAt)}
          </p>
        </div>
      </div>
      {r.message ? <blockquote className="review__quote">{r.message}</blockquote> : null}
      {r.via === "legacy_rider_link" ? <Badge variant="legacy">Via old rider link</Badge> : null}
      {handled ? (
        <p className="review__muted" role="status">
          Already handled. {handled}
        </p>
      ) : view === "blocked" ? (
        <div className="review__actions">
          <Button variant="secondary" loading={busy} loadingLabel="Unblocking…" disabled={disabled} onClick={onUnblock} aria-label={`Unblock ${name}`}>
            Unblock
          </Button>
        </div>
      ) : (
        <div className="review__actions">
          <span ref={(el) => approveRef(el?.querySelector("button") ?? null)} className="review__cell">
            <Button loading={busy} loadingLabel="Working…" disabled={disabled} onClick={() => onAct("approve")} aria-label={`Approve ${name}`}>
              Approve
            </Button>
          </span>
          <Button variant="secondary" disabled={disabled || busy} onClick={() => onAct("reject")} aria-label={`Reject ${name}`}>
            Reject
          </Button>
          <Button variant="tertiary" disabled={disabled || busy} aria-expanded={more} onClick={() => setMore((m) => !m)} aria-label={`More options for ${name}`}>
            ...
          </Button>
          {more ? (
            <Button
              variant="danger-secondary"
              disabled={disabled || busy}
              onClick={() => {
                setMore(false);
                onBlock();
              }}
            >
              Reject and block
            </Button>
          ) : null}
        </div>
      )}
    </li>
  );
}
