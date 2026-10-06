# Screen: Approved rider home ("Your trips" on `/`, and the trip as a rider)

Access model: decision-log Entry 29 and `docs/api-contract.md` (v2 `me` rows). Offline and iOS storage: Entries 18,
19.

## Design feature
A signed-in rider opening the app (usually the installed app, on the road) gets to their trip in one tap and sees
at once whether anything is waiting to send. `/` is Discover with "Your trips" on top and **no auto-redirect**
(DESIGN.md D7; confirmed by `ba` as C10). This replaces the old auto-redirect to the last slug.

## Design format — "Your trips" section on `/`
Placed above "Public trips" on Discover (see `discover.md`). Vertical auto layout, gap 12.

1. H2 "Your trips".
2. **Continue card** (the most recently opened member trip on this device, if it is still in `/me/trips`):
   `Card/trip` emphasised with a 2 px `brand/primary` border, a role badge, and a full-width primary lg "Add stop"
   button inside the card (rider or leader). The card body links to `/trips/$tripId`; the button links to
   `/trips/$tripId/add`. Two separate targets, each ≥ 48 px, 8 px apart. The id comes from device storage (a
   last-opened trip id, not a credential).
3. **Old-link continue card** (only when the device still holds a pre-upgrade `lastSlug`): compact `Card/trip`
   titled "Continue: last trip link" with the cached trip name if the persisted record has one, the `legacy` badge
   "Old trip link", linking to `/t/$slug`. No Add stop button: legacy routes are read-only (`legacy-link.md`). The
   slug itself is never shown as text.
4. Other member trips: compact `Card/trip` rows with role badges (Leader / Rider), in the order `/me/trips` returns.
5. Pending requests: rows "Waiting for approval · <trip name>" with the Pending badge, from `/me/join-requests`
   filtered to `state = pending`; each links to `/trips/$tripId/join`.
6. Leaders: a count badge on each leader trip card when it has pending requests: "3 requests to review" (link →
   Members, Requests view). `MyTripOut` carries no count, so the number is the length of
   `GET /api/v2/trips/{tripId}/join-requests` (default `state=pending`), fetched per leader trip, lazily after the
   section renders; no badge until it arrives, and none offline. See DESIGN.md §13 (open conflict X2).
7. Tertiary "Create a trip".

If the user has no member trips and no pending requests: a small EmptyState inside the section: "You're not on a
trip yet" / "Open a trip and tap Request to join, or create your own." (no big illustration; Public trips follow
right below).

## Design format — the trip as a rider
As `trip-detail.md` with `viewer.role = rider`: bottom "Add stop" bar, bike editing (online only), the "You see
stops live" notice, and "Public from …" chips on recent stops (public trips).

## States
| State | Presentation |
|---|---|
| Offline, last trip persisted | The continue card renders from the persisted `TripOut`, keyed per trip id (C14, confirmed), with the Offline notice; "Add stop" works (it only writes to the queue). The rest of the list shows "Your other trips need a connection." Only the one trip is persisted; the list itself is not (Entry 19 unchanged) |
| Offline, nothing persisted | "Your trips need a connection the first time." |
| Queue waiting | The global status stack sits above everything, so "Waiting to send" is the first thing seen |
| Queue paused on 401 | "Sign in to send 3 items" notice + Sign in (the items are not failed) |
| `/me/trips` 401 | Treated as signed out: the section is replaced by the onboarding card; `btj.me` is cleared |
| `429` | The section shows "Too many requests. Try again in N seconds." with "Try again" |
| Signed in in Safari, not in the installed iOS app | The installed app shows signed-out Discover; the onboarding card shows the iOS line |
| Revoked from the continue trip | The card drops out on refetch (it is no longer in `/me/trips`); the Failed items in the status stack explain why ("You're no longer a rider on this trip") |

## APIs called
- `GET /api/v2/auth/me` (signed-in check).
- `GET /api/v2/me/trips` (`MyTripOut[]`: active memberships with their role).
- `GET /api/v2/me/join-requests` (`MyJoinRequestOut[]`, pending rows only here).
- Leaders only: `GET /api/v2/trips/{tripId}/join-requests` per leader trip, for the count badge.
- The continue card reads the persisted `TripOut` for its trip id when offline.

## Handoff checklist
- [ ] A signed-in rider reaches Add stop from app launch in one tap; `/` never redirects.
- [ ] The card link and the Add stop button are separate targets, ≥ 48 px each, ≥ 8 px apart.
- [ ] Offline launch with a cached trip still shows the continue card and Add stop.
- [ ] Pending requests are listed with the Pending badge.
- [ ] The old-link card appears only when `lastSlug` exists, links to `/t/<slug>`, and shows no slug text and no Add stop.
