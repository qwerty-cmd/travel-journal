import type { ReactNode } from "react";
import "./TripCard.css";

// Design feature: one trip in a list (DESIGN.md §5.5 `Card/trip`), on Discover
// and in "Your trips".
// Design format: padding 16, gap 8, radius/lg, elevation/1, min 48 px. Row 1:
// the trip name as the card's only link (stretched over the whole card, so one
// Tab stop per card) + badges. Then small muted meta lines. The caller builds
// the link (a router <Link> with className "trip-card__link") so the card
// doesn't need to know the route.
// APIs called: none.
export function TripCard({ link, badges, meta }: { link: ReactNode; badges?: ReactNode; meta?: ReactNode[] }) {
  return (
    <article className="trip-card">
      <div className="trip-card__head">
        <h3 className="trip-card__name">{link}</h3>
        {badges}
      </div>
      {meta?.map((line, i) => (
        <p key={i} className="trip-card__meta">
          {line}
        </p>
      ))}
    </article>
  );
}
