# Screen: Trip detail (`/trips/$tripId`, `/timeline`, `/bikes`)

Access model: decision-log Entry 29 (ADR §1, §2, §7). Offline read path: Entry 19. Times: Entry 26.
Mockup: `docs/design/mockups/trip-detail.html`. Tokens: `docs/design/DESIGN.md`.

## Design feature
One trip, for every audience. The public sees a delay-filtered journal (map, timeline, photos, bikes). Members see
it live. Riders and leaders also get Add stop and bike editing, and leaders get Members and Settings. What renders
is decided by `TripOut.viewer.role` (`anonymous | none | pending | rider | leader`), never by device storage. The
server enforces all of it regardless.

## Design format
Frame: phone 390, vertical auto layout, `surface/page`.

1. **TopBar**: back arrow ("Back", to `/`) + trip name truncated (body 600) · Account / Sign in on the right.
2. **Status stack** (global).
3. **Trip header** (padding 16, gap 8, `surface/card`, bottom border `border/subtle`):
   - H1 trip name (title, wraps, no truncation).
   - Meta row (small, muted): "Starts 12 Oct 2026 · 3 riders" (`startDate`, `riderCount`).
   - Badge row (gap 8, wraps): visibility (`Public` / `Private`) · role badge (`Leader` / `Rider` / `Pending`,
     members and requesters only) · "Who can see this trip?" tertiary link (opens the visibility explainer dialog,
     DESIGN.md §9).
   - Leader only: settings IconButton (`settings`, "Trip settings") top-right → `/trips/$tripId/settings`.
   - Audience CTA row (see "Role variants" below).
4. **Tabs** (`trip` variant, fill width): Map · Timeline · Bikes · Members (riders and leaders; for leaders a count
   badge "3" with the accessible name "3 pending requests"). Riders get a read-only Members view with their own
   "Leave trip" (`members.md`); the contract lets any active member list members.
5. **Tab panel:**
   - **Map** (`/trips/$tripId`): delay caption (non-members of public trips with `publicDelayHours > 0`) or the
     member "You see stops live" notice (dismissable per device); map frame 60vh (DESIGN.md §4.6); below it "Latest
     stops" H2 + the 3 newest `Card/stop` rows + "See all stops" tertiary link → Timeline. For non-members the map
     draws only visible stops, and the trail only when 2 or more are visible (server rule).
   - **Timeline** (`/timeline`): same caption or notice, then an ordered list of `Card/stop`, oldest first (existing
     sort rule: by instant, ties by id). A vertical 2 px `border/subtle` rail with a dot per stop (filled = GPS,
     hollow dashed = approximate, plus the words " · approximate location"). Members of a public trip: a stop with
     `arrivedAt + publicDelayHours` in the future gets the chip "Public from 4:05 pm tomorrow ACST" (computed from
     `TripOut.publicDelayHours`; C5 resolved). Private trips show no chip.
   - **Bikes** (`/bikes`): list of `Card/bike`, sorted by rider name (existing rule). Rider/leader: an "Add bike"
     secondary button at the top, which expands the existing form inline; "Edit" per card swaps it for the same
     form. Bike writes go through the v2 routes and are **online-only, never queued** (`t-am-fe-rider-home-restyle`);
     the offline message stays "You're offline — try again when connected."
6. **BottomActionBar** (rider/leader only, Map and Timeline tabs): primary lg "Add stop" (`plus`) →
   `/trips/$tripId/add`. The page gets bottom padding = bar height.

≥ 768: header and tabs max 720 centred; map 50vh. ≥ 1024: tabs become Journey · Bikes · Members; Journey is a
split view (map 7 col sticky, timeline 5 col scrolling); "Add stop" moves into the header as primary md; no bottom bar.

### Role variants (audience CTA row in the header)
| `viewer.role` | CTA row |
|---|---|
| `anonymous` | Tertiary link "Riding on this trip? Sign in to ask to join" → `/signin?next=/trips/$tripId/join` |
| `none` (signed in, not member) | Secondary md "Request to join" → `/trips/$tripId/join` |
| `pending` | Info notice "Your request to join is waiting for a leader. You'll be able to add stops once approved." + tertiary "Cancel request" (see `join-request.md`) |
| `rider` | None in the header; bottom bar Add stop; Members tab (read-only, with Leave) |
| `leader` | None in the header; bottom bar Add stop; settings icon; Members tab (full) |

`none` covers revoked, rejected, blocked and self-departed users too (contract `ViewerRole`). The UI never tries to
tell them apart on the trip; the join screen shows the server's own `409` message if a request is refused.

## States
| State | Presentation |
|---|---|
| Loading trip | Header skeleton (2 lines + badge) + map skeleton frame |
| Cold start | + "Waking up the server…" notice |
| Offline, trip cached | Header renders from cache; Offline notice; Map tab: map frame with the grid background + "Map needs a connection" overlay text; Timeline: "Stops need a connection. Anything you added is waiting to send." Riders can still tap Add stop |
| Error, no cache | "Can't reach the server. Check your signal and try again." + Try again |
| Map error only | Map frame shows "Map unavailable" centred (existing copy); the timeline still loads |
| Stops error | Envelope message or "Couldn't load stops" + Try again (existing copy) |
| Empty (no stops, or none visible yet) | Map: Australia + overlay EmptyState "No stops yet". Timeline: EmptyState `map-pin`: "No stops yet" / non-member of a public trip with a delay: "Stops appear here <N> hours after they're added." / rider: "Tap Add stop to add the first one." |
| Empty bikes | "No bikes yet" (existing); rider: + "Add bike" |
| 404 (unknown or private for non-member) | Full-page "Trip not found" (DESIGN.md §7). The server's 404 is byte-identical for both, and the page copy depends only on the reader's own sign-in state |
| 403 on a bike write | Inline danger under the form: the envelope message ("You're not a rider on this trip." / "You're no longer a rider on this trip."); controls re-render after the trip refetch |
| 401 on a bike write | No automatic redirect (it would lose the typed input). Inline warning under the form: "Your session has ended. Sign in, then save again." + secondary "Sign in" → `/signin?next=/trips/$tripId/bikes` |
| 422 on a bike write | Inline danger under the form with the envelope message; input kept |
| 429 (read or bike write) | Warning notice "Too many requests. Try again in N seconds." (DESIGN.md §5.6 rounding) |
| Revoked while viewing | Next trip refetch yields `none` (public trip) or the 404 page (private trip): write controls disappear, and a danger notice "Your rider access was removed." appears once on a public trip; queue failures show in the status stack |
| Pending | As role variant |

Reads never return 401: the v2 reader gate answers `200` or `404` only, so an expired session on a public trip just
reads as a non-member (delayed). The trip page doesn't prompt sign-in from a read.

## APIs called
- `GET /api/v2/trips/{tripId}` (`TripOut`: `visibility`, `publicDelayHours`, `riderCount`, `lastPublicStopAt`,
  `viewer.role`, bikes). Delay-applied for non-members. Persisted per trip id for offline reading (Entry 19
  pattern; C14 confirmed) and used as `initialData`.
- `GET /api/v2/trips/{tripId}/map`, `GET /api/v2/trips/{tripId}/stops`: delay-filtered for non-members.
- `GET /api/v2/trips/{tripId}/bikes` (`BikeOut[]`, not delayed) to refresh after a bike write.
- Bikes (rider/leader, online-only): `POST /api/v2/trips/{tripId}/bikes` (`BikeCreate`, client id; `201` new,
  `200` replay, `401`, `403`, `404`, `409`, `422`, `429`) and `PATCH /api/v2/trips/{tripId}/bikes/{bikeId}`
  (`BikePatch`: send only changed fields, never `null`; `200`, `401`, `403`, `404`, `422`, `429`).
- Leader count badge: `GET /api/v2/trips/{tripId}/join-requests` (leader only; default `state=pending`), count =
  list length.
- `GET /api/v2/trips/{tripId}/members` only when the Members tab opens (`members.md`).

## Handoff checklist
- [ ] Each role in the table renders exactly its CTA; anonymous, `none` and `pending` never see Add stop, Members or
  settings; riders see Members without leader actions.
- [ ] Bike add/edit offline shows the offline message and writes nothing to the queue.
- [ ] A member of a public trip with a 24 h delay sees "Public from …" on a stop added in the last 24 h; a trip with
  delay 0 shows no chip and no viewer caption.
- [ ] Tabs are links; Back returns to the previous tab; `aria-current="page"` on the active tab.
- [ ] The approximate stop shows a hollow dashed dot **and** the words "approximate location".
- [ ] The delay caption shows for non-members of a public trip; the "You see stops live" notice for members.
- [ ] The bottom bar never covers the last timeline row (scroll to the end; the row is fully visible).
- [ ] At ≥ 1024 the split view shows map and timeline together; no bottom bar.
- [ ] The 404 text is byte-identical for an unknown id and a private trip viewed as a non-member.
