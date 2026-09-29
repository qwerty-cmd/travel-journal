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
   - Meta row (small, muted): "Starts 12 Oct 2026 · 3 riders".
   - Badge row (gap 8, wraps): visibility (`Public` / `Private`) · role badge (`Leader` / `Rider` / `Pending`,
     members and requesters only) · "Who can see this trip?" tertiary link (opens the visibility explainer dialog,
     DESIGN.md §9).
   - Leader only: settings IconButton (`settings`, "Trip settings") top-right → `/trips/$tripId/settings`.
   - Audience CTA row (see "Role variants" below).
4. **Tabs** (`trip` variant, fill width): Map · Timeline · Bikes · Members (leader; count badge "3" with name "3
   pending requests").
5. **Tab panel:**
   - **Map** (`/trips/$tripId`): delay caption (non-members of public trips) or the member "You see stops live"
     notice (dismissable per device); map frame 60vh (DESIGN.md §4.6); below it "Latest stops" H2 + the 3 newest
     `Card/stop` rows + "See all stops" tertiary link → Timeline.
   - **Timeline** (`/timeline`): same caption or notice, then an ordered list of `Card/stop`, oldest first (existing
     sort rule: by instant, ties by id). A vertical 2 px `border/subtle` rail with a dot per stop (filled = GPS,
     hollow dashed = approximate, plus the words " · approximate location"). Members: stops newer than the delay
     get a "Not public yet" / "Public from …" chip (C5).
   - **Bikes** (`/bikes`): list of `Card/bike`, sorted by rider name (existing rule). Rider/leader: an "Add bike"
     secondary button at the top, which expands the existing form inline; "Edit" per card swaps it for the same
     form. Bike writes are online-only (existing); the offline message stays "You're offline — try again when
     connected."
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
| `rider` | None in the header; bottom bar Add stop |
| `leader` | None in the header; bottom bar Add stop; settings icon; Members tab |

## States
| State | Presentation |
|---|---|
| Loading trip | Header skeleton (2 lines + badge) + map skeleton frame |
| Cold start | + "Waking up the server…" notice |
| Offline, trip cached | Header renders from cache; Offline notice; Map tab: map frame with the grid background + "Map needs a connection" overlay text; Timeline: "Stops need a connection. Anything you added is waiting to send." Riders can still tap Add stop |
| Error, no cache | "Can't reach the server. Check your signal and try again." + Try again |
| Map error only | Map frame shows "Map unavailable" centred (existing copy); the timeline still loads |
| Stops error | Envelope message or "Couldn't load stops" + Try again (existing copy) |
| Empty (no stops) | Map: Australia + overlay EmptyState "No stops yet". Timeline: EmptyState `map-pin`: "No stops yet" / non-member of public trip: "Stops appear here 24 hours after they're added." / rider: "Tap Add stop to add the first one." |
| Empty bikes | "No bikes yet" (existing); rider: + "Add bike" |
| 404 (unknown or private for non-member) | Full-page "Trip not found" (DESIGN.md §7): identical for both |
| 403 on a bike write | Inline danger under the form: envelope message; controls re-render after the trip refetch |
| 401 on a bike write | No automatic redirect (it would lose the typed input). Inline warning under the form: "Your session has ended. Sign in, then save again." + secondary "Sign in" → `/signin?next=/trips/$tripId/bikes` |
| Revoked while viewing | Next trip refetch yields `none`: write controls disappear, and a danger notice "Your rider access was removed." appears once; queue failures show in the status stack |
| Pending | As role variant |

## APIs called
- `GET /api/v2/trips/{tripId}` (ADR §2): `TripOut` incl. `visibility`, `viewer.role`, bikes. Persisted for offline
  reading (Entry 19 pattern, now keyed by trip id).
- `GET /api/v2/trips/{tripId}/map`, `GET /api/v2/trips/{tripId}/stops` (ADR §2): delay-filtered for non-members.
- Bikes: `TBC: POST /api/v2/trips/{tripId}/bikes`, `TBC: PATCH /api/v2/trips/{tripId}/bikes/{id}` (DESIGN.md §13 C2).
- Leader count badge: `TBC` pending-request count (C6).

## Handoff checklist
- [ ] Each role in the table renders exactly its CTA; anonymous and `none` never see Add stop, Members or settings.
- [ ] Tabs are links; Back returns to the previous tab; `aria-current="page"` on the active tab.
- [ ] The approximate stop shows a hollow dashed dot **and** the words "approximate location".
- [ ] The delay caption shows for non-members of a public trip; the "You see stops live" notice for members.
- [ ] The bottom bar never covers the last timeline row (scroll to the end; the row is fully visible).
- [ ] At ≥ 1024 the split view shows map and timeline together; no bottom bar.
- [ ] The 404 text is byte-identical for an unknown id and a private trip viewed as a non-member.
