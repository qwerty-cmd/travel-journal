import { useEffect, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ApiError } from "../api/client";
import { listPublicTripsApiV2TripsGetQueryOptions } from "../api/gen/hooks/useListPublicTripsApiV2TripsGet";
import { useListMyTripsApiV2MeTripsGet } from "../api/gen/hooks/useListMyTripsApiV2MeTripsGet";
import type { TripSummaryOut } from "../api/gen/types/TripSummaryOut";
import { errorText, formatRetry, isRateLimited, useMe, useSlow } from "../auth";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";
import { TripCard } from "../components/TripCard";
import { formatDate, formatInstant, formatRelative } from "../format";
import { GlobeIcon, UserIcon } from "../icons";
import { getLastSlug, loadTrip } from "../localStore";
import { parseTripLink } from "../trip";
import "./index.css";

// Design feature: Discover at `/` (docs/design/screens/discover.md, decision-log
// Entry 29). Anyone can browse public trips and open one. Signed-in users see
// "Your trips" above. The Entry 18 auto-redirect to the last slug is gone
// (DESIGN.md D7 / C10): a device that still holds `lastSlug` gets a "Continue:
// last trip link" card instead, and the paste field survives behind "Have an
// old trip link?" for old links (Entry 18's iOS first-launch case).
// Design format: top bar (wordmark; "Sign in" or the Account icon link), a
// visually hidden H1, "Your trips" (signed in, or when `lastSlug` exists),
// "Public trips" (Card/trip list, 20 per page, "Show more trips" until
// `nextCursor` is null; focus moves to the first new card), then the old-link
// footer. States: 3 skeleton cards while loading (+ "Waking up the server…"
// after 3 s), envelope / offline / 429 notice + "Try again", EmptyState when
// there are no public trips, and an inline "Couldn't load more trips" for a
// failed page (a 422 bad cursor restarts from the first page).
// APIs called: GET /api/v2/trips?cursor=&limit=20 (generated query options, one
// query per page), GET /api/v2/auth/me (useMe; a 401 just means signed out),
// GET /api/v2/me/trips (signed in only). The paste field calls nothing.
export const Route = createFileRoute("/")({
  component: Discover,
});

const PAGE_SIZE = 20;
const OFFLINE = "Can't reach the server. Check your signal and try again.";

function Discover() {
  const me = useMe();
  const signedIn = !!me.data;
  const lastSlug = getLastSlug();

  return (
    <div className="discover">
      <header className="discover__topbar">
        <span className="discover__wordmark" aria-hidden="true">
          Bike Trip Journal
        </span>
        {signedIn ? (
          <Link to="/account" className="discover__account" aria-label="Account">
            <UserIcon />
          </Link>
        ) : (
          <Link to="/signin" search={{}} className="discover__signin">
            Sign in
          </Link>
        )}
      </header>
      <main className="discover__main">
        <h1 className="visually-hidden">Discover bike trips</h1>
        {(signedIn || lastSlug) && <YourTrips signedIn={signedIn} lastSlug={lastSlug} />}
        <PublicTrips />
        <OldLink />
      </main>
    </div>
  );
}

function YourTrips({ signedIn, lastSlug }: { signedIn: boolean; lastSlug: string | null }) {
  const myTrips = useListMyTripsApiV2MeTripsGet({ query: { enabled: signedIn } });
  // The legacy record's name, if this device kept one; the slug itself is never shown.
  const legacyName = lastSlug ? loadTrip(lastSlug)?.name : undefined;

  return (
    <section className="discover__section" aria-labelledby="your-trips">
      <h2 id="your-trips" className="discover__h2">
        Your trips
      </h2>
      {lastSlug && (
        <TripCard
          link={
            <Link to="/t/$slug" params={{ slug: lastSlug }} className="trip-card__link">
              Continue: last trip link
            </Link>
          }
          badges={<Badge variant="legacy">Old trip link</Badge>}
          meta={legacyName ? [legacyName] : undefined}
        />
      )}
      {signedIn &&
        (myTrips.isPending ? (
          <p className="discover__muted">Loading your trips…</p>
        ) : myTrips.isError ? (
          <p className="discover__muted">{errorText(myTrips.error, "Your trips need a connection the first time.")}</p>
        ) : myTrips.data.length === 0 ? (
          <div>
            <p className="discover__empty-title">You're not on a trip yet</p>
            <p className="discover__muted">Open a trip and tap Request to join, or create your own.</p>
          </div>
        ) : (
          <ul className="discover__list">
            {myTrips.data.map((trip) => (
              <li key={trip.id}>
                <TripCard
                  link={
                    <Link to="/trips/$tripId" params={{ tripId: trip.id }} className="trip-card__link">
                      {trip.name}
                    </Link>
                  }
                  badges={<Badge variant={trip.role} />}
                  meta={[`Starts ${formatDate(trip.startDate)}`]}
                />
              </li>
            ))}
          </ul>
        ))}
    </section>
  );
}

function PublicTrips() {
  // One cursor per loaded page; null is the first page.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const pages = useQueries({
    queries: cursors.map((cursor) =>
      listPublicTripsApiV2TripsGetQueryOptions({
        params: cursor === null ? { limit: PAGE_SIZE } : { cursor, limit: PAGE_SIZE },
      }),
    ),
  });
  const first = pages[0];
  const last = pages[pages.length - 1];
  const slow = useSlow(first.isPending);

  const items: TripSummaryOut[] = pages.flatMap((p) => p.data?.items ?? []);

  // After "Show more trips": focus the first new card once its page arrives.
  const listRef = useRef<HTMLUListElement>(null);
  const focusFrom = useRef<number | null>(null);
  const [announce, setAnnounce] = useState("");
  useEffect(() => {
    const from = focusFrom.current;
    if (from === null || !last.isSuccess) return;
    focusFrom.current = null;
    const added = items.length - from;
    setAnnounce(`${added} more ${added === 1 ? "trip" : "trips"} loaded`);
    listRef.current?.querySelectorAll<HTMLAnchorElement>(".trip-card__link")[from]?.focus();
  }, [last.isSuccess, items.length]);

  const showMore = () => {
    const next = last.data?.nextCursor;
    if (!next) return;
    focusFrom.current = items.length;
    setCursors((cs) => [...cs, next]);
  };

  let body;
  if (first.isPending) {
    body = (
      <div aria-busy="true" className="discover__list">
        <span className="visually-hidden">Loading…</span>
        {[0, 1, 2].map((i) => (
          <div key={i} className="discover__skeleton" />
        ))}
      </div>
    );
  } else if (first.isError) {
    body = <ReadError error={first.error} onRetry={() => first.refetch()} />;
  } else if (items.length === 0) {
    body = (
      <div className="discover__emptystate">
        <span className="discover__emptyicon">
          <GlobeIcon />
        </span>
        <p className="discover__empty-title">No public trips yet</p>
        <p className="discover__muted">When a trip is made public, it appears here.</p>
      </div>
    );
  } else {
    body = (
      <>
        <ul className="discover__list" ref={listRef}>
          {items.map((trip) => (
            <li key={trip.id}>
              <PublicTripCard trip={trip} />
            </li>
          ))}
        </ul>
        {pages.length > 1 && last.isError ? (
          <div className="discover__more-error">
            <p className="discover__danger">Couldn't load more trips</p>
            <Button
              variant="secondary"
              onClick={() => {
                // A cursor the server didn't issue (422) can't be retried: start over.
                if (last.error instanceof ApiError && last.error.status === 422) setCursors([null]);
                else last.refetch();
              }}
            >
              Try again
            </Button>
          </div>
        ) : (
          (last.isPending || last.data?.nextCursor) && (
            <Button
              variant="secondary"
              className="discover__more"
              loading={last.isPending}
              loadingLabel="Loading more trips…"
              onClick={showMore}
            >
              Show more trips
            </Button>
          )
        )}
      </>
    );
  }

  return (
    <section className="discover__section" aria-labelledby="public-trips">
      <h2 id="public-trips" className="discover__h2">
        Public trips
      </h2>
      <p className="discover__muted">Public trips, most recently updated first.</p>
      {slow && <StatusNotice tone="info" title="Waking up the server…" detail="The first visit after a quiet spell can take a little while." />}
      {body}
      <p className="visually-hidden" role="status">
        {announce}
      </p>
    </section>
  );
}

function PublicTripCard({ trip }: { trip: TripSummaryOut }) {
  const riders = `${trip.riderCount} ${trip.riderCount === 1 ? "rider" : "riders"}`;
  return (
    <TripCard
      link={
        <Link to="/trips/$tripId" params={{ tripId: trip.id }} className="trip-card__link">
          {trip.name}
        </Link>
      }
      badges={<Badge variant="public" />}
      meta={[
        `Started ${formatDate(trip.startDate)} · ${riders}`,
        trip.lastPublicStopAt ? (
          <>
            Last public stop{" "}
            <time dateTime={trip.lastPublicStopAt} title={formatInstant(trip.lastPublicStopAt)}>
              {formatRelative(trip.lastPublicStopAt)}
            </time>
          </>
        ) : (
          "No public stops yet"
        ),
      ]}
    />
  );
}

function ReadError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const limited = isRateLimited(error) && error instanceof ApiError && error.retryAfter !== undefined;
  return (
    <div className="discover__error">
      {limited ? (
        <StatusNotice tone="warning" title={`Too many requests. ${formatRetry((error as ApiError).retryAfter!)}`} />
      ) : (
        <StatusNotice tone="danger" title={errorText(error, OFFLINE)} />
      )}
      <Button variant="secondary" onClick={onRetry}>
        Try again
      </Button>
    </div>
  );
}

function OldLink() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [link, setLink] = useState("");
  const [invalid, setInvalid] = useState(false);

  return (
    <footer className="discover__footer">
      <Button variant="tertiary" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        Have an old trip link?
      </Button>
      {open && (
        <form
          className="discover__paste"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            const slug = parseTripLink(link);
            setInvalid(!slug);
            if (slug) navigate({ to: "/t/$slug", params: { slug } });
          }}
        >
          <TextField
            label="Paste your old trip link"
            value={link}
            onChange={(e) => setLink(e.target.value)}
            error={invalid ? "That doesn't look like a trip link." : undefined}
          />
          <Button type="submit" variant="secondary">
            Open trip
          </Button>
        </form>
      )}
    </footer>
  );
}
