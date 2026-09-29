# Screen: Approved rider home ("Your trips" on `/`, and the trip as a rider)

Access model: decision-log Entry 29. Offline and iOS storage: Entries 18, 19.

## Design feature
A signed-in rider opening the app (usually the installed app, on the road) gets to their trip in one tap and sees
at once whether anything is waiting to send. This replaces the old auto-redirect to the last slug (DESIGN.md D7).

## Design format — "Your trips" section on `/`
Placed above "Public trips" on Discover (see `discover.md`). Vertical auto layout, gap 12.

1. H2 "Your trips".
2. **Continue card** (the most recently opened member trip on this device, if any): `Card/trip` emphasised with a 2
   px `brand/primary` border, a role badge, and a full-width primary lg "Add stop" button inside the card (rider or
   leader). The card body links to the trip; the button links to `/trips/$tripId/add`. Two separate targets, each
   ≥ 48 px, 8 px apart.
3. Other member trips: compact `Card/trip` rows with role badges (Leader / Rider), sorted by last activity.
4. Pending requests: rows "Waiting for approval · <trip name>" with the Pending badge; link to `/trips/$tripId/join`.
5. Leaders: a count badge on the card when their trip has pending requests: "3 requests to review" (link → Members).
6. Tertiary "Create a trip".

If the user has no member trips and no requests: a small EmptyState inside the section: "You're not on a trip yet" /
"Open a trip and tap Request to join, or create your own." (no big illustration; Public trips follow right below).

## Design format — the trip as a rider
As `trip-detail.md` with `viewer.role = rider`: bottom "Add stop" bar, bike editing, the "You see stops live"
notice, and "Not public yet" chips on recent stops (public trips).

## States
| State | Presentation |
|---|---|
| Offline, last trip persisted | The continue card renders from the persisted trip record (today's Entry 19 pattern, keyed by trip id) + Offline notice; "Add stop" works (it only writes to the queue). The rest of the list shows "Your other trips need a connection." Persisting the whole list would extend the offline read path, so it's flagged (DESIGN.md §13 C14), not assumed |
| Offline, nothing persisted | "Your trips need a connection the first time." |
| Queue waiting | The global status stack sits above everything, so "Waiting to send" is the first thing seen |
| 401 with queued items | "Sign in to send 3 items" notice + Sign in |
| Signed in in Safari, not in the installed iOS app | The installed app shows signed-out Discover; the onboarding card shows the iOS line |
| Revoked from the continue trip | The card drops out on refetch; the Failed items in the status stack explain why ("You're no longer a rider on this trip") |

## APIs called
`TBC: GET /api/v2/me/trips` (DESIGN.md §13 C3), `TBC: GET /api/v2/auth/me`; the continue card id comes from device
storage (a last-opened trip id, not a credential).

## Handoff checklist
- [ ] A signed-in rider reaches Add stop from app launch in one tap.
- [ ] The card link and the Add stop button are separate targets, ≥ 48 px each, ≥ 8 px apart.
- [ ] Offline launch with a cached trip still shows the continue card and Add stop.
- [ ] Pending requests are listed with the Pending badge.
